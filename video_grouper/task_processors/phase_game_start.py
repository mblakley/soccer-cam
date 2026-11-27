"""Phase-detection game-start resolver (decisions 1, 7, 8).

This is the default way game start is found. When
``[PROCESSING] game_start_method == "phase_detection"`` AND the recording camera
is a whistle-capable Reolink, run the offline game-phase detector on the combined
video and — if the detected kickoff is ``ko_trustworthy`` — write
``match_info.start_time_offset`` from it, minus a small
:data:`PHASE_KO_TRIM_BACKUP_SECONDS` safety backup. The caller then skips the
NTFY game-start walk.

The gate is ``ko_trustworthy`` (KO-specific), not ``ok`` (whole-fit): we only
trim on KO, so a game with an exact kickoff-whistle KO but a failed HT/2H is
still auto-trimmed (05.27 / 06.06-Sullivan). The backup is far smaller than the
NTFY walk's ``GAME_START_BACKUP_SECONDS`` (240s, tied to the 5-minute poll):
every trusted, non-truncated KO measured is within 60s (worst -42s, all early),
so 60s keeps the trim safe while leaving far less pre-game warm-up.

Truncation trade-off (accepted, decision 2026-07-01): a truncated-start recording
reads as ``ko_trustworthy`` but anchors a mid-first-half whistle as "kickoff" —
indistinguishable from a real kickoff on every available signal (no reliable
schedule; no in-video separator). Rather than force an attention-grab on every
game to catch a rare case, we auto-proceed on trust and rely on the post-detection
verify loop / a viewer to catch the rare miss.

Falls back (returns ``False`` — the caller runs the NTFY walk unchanged) for:

* ``game_start_method == "ntfy"`` (manual flow forced),
* a Dahua camera (decision 7 — no usable whistle audio),
* a ``None`` / sanity-gate-rejected (``ok == False``) detector fit,
* any unexpected error (detection can never make game start worse than today).

The detector dependency (onnxruntime / cv2 / av) is imported lazily inside
:func:`_run_detector` so importing this module stays cheap in bundles that lack
the inference stack — only the actual Reolink + phase-detection path needs it.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# Trim safety backup applied to the detected kickoff, decoupled from the NTFY
# walk's 240s (decision 8, amended 2026-07-01). The detector's trusted KO error
# is early/tiny (worst observed −42s), so 60s stays trim-safe with margin while
# keeping ~3 min less warm-up than the inherited 4-min pad. Env-overridable
# (mirrors the phase_detector.py knobs); the ``truncated_start`` guard in the
# caller still blocks the only late-KO source.
PHASE_KO_TRIM_BACKUP_SECONDS = int(os.environ.get("PHASE_KO_TRIM_BACKUP_SECONDS", "60"))


# Hard ceiling on one detector run. The detector is a legitimate multi-minute
# compute (a 96-min 4K combined video measured 7m54s in production on
# 2026-08-03), so this is a safety valve for a genuinely wedged run, not a
# latency budget: on expiry we log an ERROR and fall back to the NTFY walk (the
# documented degraded path — never worse than manual), and detection is not
# restarted for that game in this process. 0 (or any value <= 0) disables the
# ceiling; a non-numeric value keeps the default.
def _env_seconds(name: str, default: float) -> float:
    """A non-negative seconds setting; a bad value falls back to the default
    and a value <= 0 means disabled (0) instead of breaking the import."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.error("%s=%r is not a number; using %s", name, raw, default)
        return default
    return max(value, 0.0)


PHASE_DETECT_TIMEOUT_SECONDS = _env_seconds("PHASE_DETECT_TIMEOUT_SECONDS", 3600)

# Ceiling on the (optional) TTT event-tap fetch that precedes a run, so a slow
# network can't hold the single-flight run open; on expiry: no hints.
ANCHOR_FETCH_TIMEOUT_SECONDS = 90

# In-flight detector runs keyed by normalized group dir. The post-combine event
# hook and the StateAuditor's audit can request the same group within the same
# second (observed 2026-08-03 11:47:05/06: two concurrent ~8-min runs decoding
# the same 15 GB video); duplicates await the first run instead of starting
# another.
_INFLIGHT: dict = {}

# Groups whose detector run hit PHASE_DETECT_TIMEOUT_SECONDS but whose decode is
# still running: asyncio cannot interrupt the worker thread that ``to_thread``
# started, so the decode keeps going after the timeout. While a group is in
# this set, a new trigger falls straight back to the NTFY walk instead of
# starting a second decode next to the abandoned one; the entry clears itself
# when that thread finally finishes.
_ORPHANED: set = set()

# Groups with a decode running right now, whichever path started it (the
# main run or a truncated_start / truncated_end re-run): at most one decode of
# a game's video at a time.
_DECODING: set = set()

# Groups whose run timed out in this process: detection is not restarted for
# them -- the NTFY walk already owns the game start (a rerun would take as long
# again and could race the answer).
_GAVE_UP: set = set()


def _decode_busy(key: str) -> bool:
    return key in _DECODING or key in _ORPHANED


def _orphan(key: str, detector: asyncio.Future) -> None:
    """``detector`` was abandoned (timeout or cancellation) but its thread is
    still decoding: keep the group marked until it really ends."""
    _ORPHANED.add(key)

    def _release(t: asyncio.Future, _k: str = key) -> None:
        _ORPHANED.discard(_k)
        if not t.cancelled():
            t.exception()  # retrieve it so asyncio doesn't log it as unhandled

    detector.add_done_callback(_release)


async def _guarded_rerun(group_dir: str, combined_video_path: str, **flags):
    """A truncated_* re-run through the one-decode-per-game guard: skipped
    (None) while another decode of this game is running or abandoned."""
    key = _inflight_key(group_dir)
    if _decode_busy(key):
        logger.warning(
            "phase game-start: a decode of %s is already running; skipping the "
            "%s re-run (phases keep their current values)",
            group_dir,
            ", ".join(flags) or "detector",
        )
        return None
    _DECODING.add(key)
    try:
        return await _run_detector(group_dir, combined_video_path, **flags)
    finally:
        _DECODING.discard(key)


def _inflight_key(group_dir: str) -> str:
    return os.path.normcase(os.path.abspath(group_dir))


def _camera_is_reolink(config) -> bool:
    """True iff a configured camera is a Reolink.

    Reads the structured ``cameras`` list on a real pydantic Config, and falls
    back to the ``[CAMERA] type`` section accessor used by configparser-based
    test configs. Defensive: never raises on an unusual config shape.
    """
    cams = getattr(config, "cameras", None)
    types: list[str] = []
    if isinstance(cams, list) and cams:
        types = [getattr(c, "type", "") or "" for c in cams]
    else:
        cam = getattr(config, "camera", None)
        ctype = getattr(cam, "type", None)
        if isinstance(ctype, str) and ctype:
            types = [ctype]
    return any(t.strip().lower() == "reolink" for t in types)


def phase_game_start_enabled(config) -> bool:
    """True iff config selects phase-detection game start for a Reolink camera."""
    processing = getattr(config, "processing", None)
    method = getattr(processing, "game_start_method", None) or "phase_detection"
    if method != "phase_detection":
        return False
    return _camera_is_reolink(config)


def _format_offset(seconds: int) -> str:
    """Format whole seconds as the ``MM:SS`` offset the NTFY walk writes."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _load_field_polygon(group_dir: str, combined_video_path: str):
    """Best-effort field polygon for the combined video.

    Reuses a ``field_polygon.json`` artifact if one already exists in the group
    dir (e.g. from a prior field_detect run); otherwise returns the neutral
    full-frame rectangle from the video's own dimensions — the same "the field
    IS the frame" default field_detect emits when no model is configured. So the
    detector always has a usable polygon. Returns ``None`` only if the video
    dimensions can't be read.
    """
    import json

    for root, _dirs, files in os.walk(group_dir):
        if "field_polygon.json" in files:
            try:
                with open(
                    os.path.join(root, "field_polygon.json"), encoding="utf-8"
                ) as f:
                    poly = json.load(f).get("polygon")
                if poly:
                    return poly
            except (OSError, json.JSONDecodeError, ValueError):
                pass
            break

    try:
        import av

        with av.open(combined_video_path) as container:
            stream = container.streams.video[0]
            width = stream.codec_context.width
            height = stream.codec_context.height
        return [
            [0.0, 0.0],
            [float(width), 0.0],
            [float(width), float(height)],
            [0.0, float(height)],
        ]
    except Exception as e:  # noqa: BLE001 — degrade to no-polygon -> NTFY fallback
        logger.warning(
            "phase game-start: could not read dimensions of %s: %s",
            combined_video_path,
            e,
        )
        return None


async def _run_detector(
    group_dir: str,
    combined_video_path: str,
    *,
    truncated_start: bool = False,
    truncated_end: bool = False,
    anchors: dict | None = None,
) -> dict | None:
    """Load the polygon and run the detector off the event loop.

    ``anchors`` (optional) is the {boundary: Anchor} map of parent event-tap
    priors the caller fetched from TTT (see maybe_resolve_phase_game_start)."""
    import asyncio
    from functools import partial

    from video_grouper.inference.phase_detector import detect_phases

    polygon = _load_field_polygon(group_dir, combined_video_path)
    if not polygon:
        return None
    return await asyncio.to_thread(
        partial(
            detect_phases,
            combined_video_path,
            polygon,
            truncated_start=truncated_start,
            truncated_end=truncated_end,
            anchors=anchors,
        )
    )


def _group_recording_start(group_dir: str, storage_path: str | None):
    """Wall-clock of the recording's first file (video time 0), or None.

    Best-effort: parent taps prefer a TTT-computed ``video_time_seconds`` anyway,
    so this only feeds the wall-clock fallback in build_anchors."""
    try:
        from video_grouper.models import DirectoryState

        state = DirectoryState(group_dir, storage_path)
        first = state.get_first_file()
        return first.start_time if first is not None else None
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _fetch_event_tap_anchors(config, group_dir: str, storage_path: str | None) -> dict:
    """Parent event-tap phase anchors for this recording's game session, or {}.

    ONLY fetches when the install is logged in as a TTT user: reuses
    ``_authed_client_and_session`` (which returns a client only when
    ``client.is_authenticated()``), so a non-TTT / logged-out install never calls
    TTT and runs detector-only. Runs sync (call from a thread). Best-effort: any
    failure -> {} (never worse than the detector alone)."""
    try:
        from video_grouper.inference.event_tap_anchors import build_anchors
        from video_grouper.task_processors.phase_ttt_push import (
            _authed_client_and_session,
        )

        ttt = getattr(config, "ttt", None)
        _dump = getattr(ttt, "model_dump", None)
        ttt_config = _dump() if callable(_dump) else ttt
        client, session = _authed_client_and_session(
            ttt_config, group_dir, str(storage_path or "")
        )
        if client is None or session is None:
            return {}  # TTT disabled, not logged in, or no session -> no fetch
        raw = client.get_sync_anchors(session["id"]) or []
        taps = [
            a
            for a in raw
            if isinstance(a, dict) and a.get("anchor_type") == "event_tap"
        ]
        recording_start = _group_recording_start(group_dir, storage_path)
        anchors = build_anchors(taps, recording_start)
        if anchors:
            logger.info(
                "phase anchors: %d parent event-taps -> %s",
                len(taps),
                {b: (a.confidence, round(a.video_time, 1)) for b, a in anchors.items()},
            )
        return anchors
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        logger.warning("event-tap anchor fetch failed (non-fatal): %s", e)
        return {}


def _phase_payload(result: dict) -> dict:
    """Shape a detector result into the persisted/pushed phases payload (carries the
    truncated_start/end flags the detector set)."""
    return {
        "source": "phase_fused",
        "ok": bool(result.get("ok")),
        "times": {
            k: float(v) for k, v in (result.get("times") or {}).items() if v is not None
        },
        "reasons": list(result.get("reasons") or []),
        "used": result.get("used"),
        "truncated_start": bool(result.get("truncated_start")),
        "truncated_end": bool(result.get("truncated_end")),
    }


async def _persist_and_push(config, group_dir, storage_path, result) -> None:
    """Persist the fused phases to group state and best-effort push them to TTT."""

    _persist_phases(group_dir, storage_path, result)
    try:
        from video_grouper.integrations import active

        await active().recordings.phases(group_dir, _phase_payload(result))
    except Exception as e:  # noqa: BLE001 — the report is best-effort
        logger.warning(
            "phase game-start: truncated TTT push failed for %s: %s", group_dir, e
        )


async def resolve_truncated_start(
    group_dir: str,
    combined_video_path: str,
    config,
    storage_path: str | None = None,
) -> bool:
    """Handle the NTFY "already started" answer on the 0:00 game-start question.

    The game was already in progress when recording began (arrived late = truncated
    start), so there is nothing to trim off the front: set ``start_time_offset = 0``
    and re-run the detector with ``truncated_start=True`` (KO pinned to 0, HT/2H/END
    still detected) so the persisted + pushed phases are correct rather than anchored
    to a bogus mid-first-half whistle. Always writes the offset; the re-run + push are
    best-effort. Returns True.

    (Perf note: the re-run recomputes signals (~minutes); a follow-up can cache the
    phase_detect step's signals so this re-fuses instantly.)
    """
    from video_grouper.models import MatchInfo

    # Trim at 0 -- keep everything; the game is already underway at the file head.
    MatchInfo.update_game_times(
        group_dir, start_time_offset="00:00", storage_path=storage_path
    )

    if not combined_video_path or not os.path.exists(combined_video_path):
        return True

    try:
        result = await _guarded_rerun(
            group_dir, combined_video_path, truncated_start=True
        )
    except Exception as e:  # noqa: BLE001 — never let the re-run break the flow
        logger.warning(
            "phase game-start: truncated_start re-run failed for %s: %s", group_dir, e
        )
        result = None

    if result:
        await _persist_and_push(config, group_dir, storage_path, result)
    logger.info(
        "phase game-start: truncated_start resolved for %s (trim 0, phases re-run)",
        group_dir,
    )
    return True


async def resolve_truncated_end(
    group_dir: str,
    combined_video_path: str,
    config,
    storage_path: str | None = None,
) -> bool:
    """Handle "ended early" — the recording cut off before full-time (e.g. a dead
    battery). Re-run the detector with ``truncated_end=True`` (END pinned to the file
    end; KO/HT/2H unchanged) and persist + push. The start trim is left as-is — only
    the end boundary moves. Best-effort; returns True."""
    if not combined_video_path or not os.path.exists(combined_video_path):
        return False

    try:
        result = await _guarded_rerun(
            group_dir, combined_video_path, truncated_end=True
        )
    except Exception as e:  # noqa: BLE001 — never let the re-run break the flow
        logger.warning(
            "phase game-start: truncated_end re-run failed for %s: %s", group_dir, e
        )
        result = None

    if result:
        await _persist_and_push(config, group_dir, storage_path, result)
    logger.info(
        "phase game-start: truncated_end resolved for %s (END = file end)", group_dir
    )
    return True


async def maybe_resolve_phase_game_start(
    group_dir: str,
    combined_video_path: str,
    config,
    storage_path: str | None = None,
) -> bool:
    """Try to set game start from phase detection. Returns True iff it did.

    On True: ``match_info.start_time_offset`` has been written from the detected
    kickoff minus :data:`PHASE_KO_TRIM_BACKUP_SECONDS`, and the fused phases are
    persisted to the group state (source ``phase_fused``); the caller MUST skip
    the NTFY game-start walk. On False: nothing was written and the caller runs
    the NTFY walk exactly as before.
    """
    if not phase_game_start_enabled(config):
        return False

    from video_grouper.models import MatchInfo

    # Already resolved (an earlier pass set it) — treat as handled so we neither
    # re-run the expensive detector nor fall through to the NTFY walk.
    existing, _ = MatchInfo.get_or_create(group_dir, storage_path)
    if existing and existing.start_time_offset and existing.start_time_offset.strip():
        return True

    if not combined_video_path or not os.path.exists(combined_video_path):
        return False

    import asyncio

    # One detector run per group at a time: duplicate triggers (post-combine
    # hook + StateAuditor firing together) share the first run's result instead
    # of decoding the same multi-GB video twice in parallel.
    key = _inflight_key(group_dir)
    if key in _GAVE_UP:
        logger.info(
            "phase game-start: detection timed out earlier for %s; the NTFY walk "
            "owns its game start",
            group_dir,
        )
        return False
    task = _INFLIGHT.get(key)
    if task is not None and task.get_loop() is not asyncio.get_running_loop():
        _INFLIGHT.pop(key, None)  # left over from a closed event loop
        task = None
    if task is None and _decode_busy(key):
        logger.warning(
            "phase game-start: a detector run is still decoding %s; not starting "
            "another — falling back to the NTFY walk",
            group_dir,
        )
        return False
    if task is None:
        task = asyncio.create_task(
            _detect_and_resolve(group_dir, combined_video_path, config, storage_path)
        )
        _INFLIGHT[key] = task

        def _clear_inflight(_t: asyncio.Task, _k: str = key) -> None:
            _INFLIGHT.pop(_k, None)

        task.add_done_callback(_clear_inflight)
    else:
        logger.info(
            "phase game-start: detection already in flight for %s; "
            "awaiting that run instead of starting another",
            group_dir,
        )
    # shield: a cancelled duplicate caller must not cancel the shared run.
    return await asyncio.shield(task)


async def _detect_and_resolve(
    group_dir: str,
    combined_video_path: str,
    config,
    storage_path: str | None,
) -> bool:
    """One full detector run + offset write for a group (single-flight body).

    Never raises: every failure path logs and returns False (the caller falls
    back to the NTFY walk, which can never be worse than today)."""
    import asyncio
    import time

    from video_grouper.models import MatchInfo

    # Parent event-tap anchors (best-effort, off the loop). Only fetched when
    # this install is a logged-in TTT user; {} otherwise -> detector-only.
    try:
        anchors = await asyncio.wait_for(
            asyncio.to_thread(
                _fetch_event_tap_anchors, config, group_dir, storage_path
            ),
            timeout=ANCHOR_FETCH_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("phase game-start: anchor fetch timed out; detector only")
        anchors = {}

    # The detector is a multi-minute compute on a full game (measured 7m54s for
    # a 96-min combined video). Announce it so an operator watching the log can
    # tell "running" from "lost" — before this line existed the run was
    # invisible until it finished (2026-08-03 incident).
    logger.info(
        "phase game-start: running the phase detector on %s "
        "(multi-minute compute on a full game; timeout %ss)",
        combined_video_path,
        PHASE_DETECT_TIMEOUT_SECONDS or "disabled",
    )
    t0 = time.monotonic()
    key = _inflight_key(group_dir)
    _DECODING.add(key)
    detector = asyncio.ensure_future(
        _run_detector(group_dir, combined_video_path, anchors=anchors)
    )

    def _decoded(_t: asyncio.Future, _k: str = key) -> None:
        _DECODING.discard(_k)

    detector.add_done_callback(_decoded)
    try:
        # shield: on timeout wait_for cancels only the wrapper, so ``detector``
        # still completes and can clear the orphan marker below.
        result = await asyncio.wait_for(
            asyncio.shield(detector),
            timeout=PHASE_DETECT_TIMEOUT_SECONDS or None,
        )
    except asyncio.CancelledError:
        # The shared run was cancelled (shutdown/restart) but the decode thread
        # keeps going: mark it so nothing starts a second decode beside it.
        _orphan(key, detector)
        raise
    except TimeoutError:
        _orphan(key, detector)
        _GAVE_UP.add(key)
        logger.error(
            "phase game-start: detector exceeded %ss for %s — abandoning phase "
            "detection, falling back to the NTFY walk (the decode thread cannot be "
            "interrupted; new triggers for this group skip detection until it ends)",
            PHASE_DETECT_TIMEOUT_SECONDS,
            group_dir,
        )
        return False
    except Exception as e:  # noqa: BLE001 — never let detection break the flow
        logger.warning(
            "phase game-start: detector failed for %s after %.1fs: %s",
            group_dir,
            time.monotonic() - t0,
            e,
        )
        return False
    elapsed = time.monotonic() - t0

    # Auto-trim gate = ``ko_trustworthy``, NOT ``ok`` (decision 2026-07-01). We only
    # trim on KO, so the KO-specific trust flag is the right gate: it accepts a game
    # with an exact kickoff-whistle KO even when HT/2H/END failed the full-fit sanity
    # (``ok`` is False) -- e.g. 05.27 / 06.06-Sullivan (KO -1s, ok=False, localized).
    # It still rejects a KO from the symmetric prior (03.21, no whistle) or a non-
    # localized warm-up whistle (05.30). Every trusted, non-truncated KO measured is
    # within 60s (worst -42s, all early), so PHASE_KO_TRIM_BACKUP_SECONDS keeps them
    # trim-safe.
    #
    # TRUNCATION TRADE-OFF (accepted): a truncated-start recording anchors a mid-
    # first-half whistle as "kickoff" and reads as ko_trustworthy (05.09 / 06.06-
    # Fairport) -- indistinguishable from a real kickoff on every available signal
    # (no reliable schedule; no in-video separator -- field-dip, block-onset, and
    # structural asymmetry all overlap; verified). Getting a human's attention is the
    # expensive step, so rather than force a confirmation on every game to catch a
    # rare case, we auto-proceed on trust and let the post-detection verify loop (S3)
    # or a viewer catch the rare miss. Attention is spent only when the detector is
    # itself unsure (ko_trustworthy False -> the NTFY walk below).
    if not result or not result.get("ko_trustworthy"):
        logger.info(
            "phase game-start: detector finished in %.1fs; KO not trustworthy for %s "
            "(falling back to the NTFY walk)",
            elapsed,
            group_dir,
        )
        return False

    times = result.get("times") or {}
    kickoff = times.get("kickoff")
    if kickoff is None:
        return False

    start_seconds = max(0, int(round(float(kickoff))) - PHASE_KO_TRIM_BACKUP_SECONDS)
    offset = _format_offset(start_seconds)

    MatchInfo.update_game_times(
        group_dir, start_time_offset=offset, storage_path=storage_path
    )
    _persist_phases(group_dir, storage_path, result)

    logger.info(
        "phase game-start: set start_time_offset=%s for %s "
        "(kickoff=%.1fs - %ds backup; detector took %.1fs)",
        offset,
        group_dir,
        float(kickoff),
        PHASE_KO_TRIM_BACKUP_SECONDS,
        elapsed,
    )
    return True


def _persist_phases(group_dir: str, storage_path: str | None, result: dict) -> None:
    """Persist the fused phases to the group state (source ``phase_fused``).

    Best-effort: a failure here must not undo the start-time write above.
    """
    try:
        from video_grouper.models import DirectoryState

        times = result.get("times") or {}
        payload = {
            "source": "phase_fused",
            "ok": bool(result.get("ok")),
            "times": {k: float(v) for k, v in times.items() if v is not None},
            "reasons": list(result.get("reasons") or []),
            "used": result.get("used"),
        }
        DirectoryState(group_dir, storage_path).set_game_phases(payload)
    except Exception as e:  # noqa: BLE001 — state persistence is best-effort
        logger.warning(
            "phase game-start: could not persist phases for %s: %s", group_dir, e
        )
