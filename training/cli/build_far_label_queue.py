r"""Build a far-label review SET for a game — active-learning selection of the *hard* frames.

Consolidates the ad-hoc ``G:\ballresearch\build_*farlabel*`` / ``build_0615_*`` one-off scripts into
ONE committed, parameterized CLI. Selects the frames where AutoCam loses or doubts the ball — which
(per Mark) is usually **occlusion** (ball behind a player), plus far-field and distractor ambiguity —
extracts full-frame strips, and writes the ``manifest.json`` the annotation server + ``far-label.html``
already consume (``D:/training_data/far_label/<set>/{manifest.json, strips/f######.jpg}``). It NEVER
writes ``labels.json`` (server-owned).

Selection criteria (``--criteria``):
  * ``lost``       — no on-field AutoCam detection that frame (ball lost = occlusion / gone-far).
  * ``lowconf``    — top on-field detection confidence in ``[lc_lo, lc_hi)`` (detector unsure).
  * ``distractor`` — 2+ on-field candidates of similar confidence (which one is the game ball?).
  * ``hard``       — the union (default): the frames AutoCam struggles with, the high-value tail.
  * ``near``       — CLOSE-ball GT by POSITION relative to the field outline (Mark 2026-07-10:
    "do it based on position, not size"). Selects the teacher-snap ball when it is near the NEAR
    touchline (``--near-depth``) and toward the frame front (biggest, close balls — the near-detection
    weak spot). ``--near-span`` expands each to a 2-4 frame arc. Reasons: ``near_misrank`` (also
    demoted by our detector), ``near_close`` (teacher top-ranked), ``near_unknown`` (no teacher).
  * ``kick``       — close-ball ARC miner: a fast in-field exit + a detection gap (a ball struck
    into an arc the detector drops), served as a WIDE bracketed window (lead-up -> flight -> landing),
    full-frame no-seed. Needs the full selector track (misses only appear under physical transitions).
  * ``diverge``    — TRACK-AUDIT sets (Mark 2026-07-06): run the current tracker over the fullgame
    dump and flag (a) human labels the track disagrees with (bad label OR bad track — re-verify),
    (b) ``teleport`` world-jumps in the raw selection (identity switches the smoother hides), and
    (c) sustained ``trackmiss`` runs (low-confidence stretches worth filling in). Every anchor is
    expanded with before/after context frames so the annotator can SEE the track; the hint marker
    is the track's own position at that frame.
  * ``disagree``   — ADJUDICATION frames (EXP-DIST-36): tier-B benchmark rows where OUR planned
    camera and AutoCam's claimed ball position disagree (one anchor per disagreement run, longest
    runs first). Hint = AutoCam's claim; context dots = our candidates + our camera center. The
    human click decides who was actually right — measured tier-B contamination is ~6.6%, and we
    were on the real ball in 31/36 contaminated frames sampled.
  * ``spans``      — CONTIGUOUS eval spans (``--windows "lo-hi,lo-hi,..."`` global frames, stride-8
    grid): run-structure GT for the continuity/viewport instrument. Isolated hard frames can score
    per-frame hits but cannot measure fragments/excursions; contiguous spans can. Built for the
    HELD-OUT games (eval-only — the training guard excludes them), so no dedupe against existing
    labels: contiguity IS the point.

Frames are restricted to active play (``game_state``), temporally spread into ``--max-frames`` bins
(most-ambiguous per bin, so no clustering), and any frames already in ``--exclude-sets`` are skipped.

    python -m training.cli.build_far_label_queue \
        --game-dir "F:/Heat_2012s/2026.06.07 - vs Lakefront SC (home)" \
        --criteria hard --max-frames 160
    # add --analyze to print the selection stats + yields WITHOUT decoding/writing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from training.data_prep import distill_dataset as dd


def _in_field(poly_np: np.ndarray, x: float, y: float, margin: float) -> bool:
    import cv2

    return cv2.pointPolygonTest(poly_np, (float(x), float(y)), True) >= margin


def _spread_bins(pool: list[list], target: int) -> list[list]:
    """Temporally spread ``pool`` rows ``[frame, ..., value]`` into ``target`` bins,
    keeping the highest-value row per bin (no clustering)."""
    if not pool:
        return []
    pool.sort(key=lambda r: r[0])
    fmin, fmax = pool[0][0], pool[-1][0]
    span = max(1, fmax - fmin)
    bins: dict[int, list] = {}
    for c in pool:
        b = int((c[0] - fmin) / span * (target - 1))
        if b not in bins or c[-1] > bins[b][-1]:
            bins[b] = c
    return sorted(bins.values(), key=lambda r: r[0])


def select_near_frames(
    ef: list[int],
    cands: dict[int, list],
    labels: dict[int, tuple[int, float]],
    geom,
    *,
    near_depth: float,
    target: int,
    exclude: set[int],
) -> list[dict]:
    """CLOSE-ball miner by POSITION relative to the field outline (Mark 2026-07-10).

    The geometric ball-SIZE estimate is useless here (an airborne ball reads tiny, and
    it maxes at ~23px), so select close balls by POSITION: near the NEAR touchline and
    toward the frame front. Gates the teacher-snap ball by ``depth`` (``(y-yf)/(yn-yf)``,
    1 = near touchline) ``>= near_depth`` and ranks by image-y so the frontmost/closest
    ball wins each temporal bin (the touchline meets the frame edge at the front, where
    balls are biggest). ``near_misrank`` = the ball our detector also demoted below a
    distractor; ``near_close`` = teacher-backed and top-ranked; ``near_unknown`` = a
    near-touchline candidate with no teacher coverage.
    """
    poly = np.asarray(getattr(geom, "polygon", np.zeros((0, 2))), float)
    if len(poly) >= 10:
        yn = float(np.mean(poly[0:5, 1]))
        yf = float(np.mean(poly[5:10, 1]))
    else:
        yn = float(poly[:, 1].max()) if len(poly) else 1.0
        yf = float(poly[:, 1].min()) if len(poly) else 0.0
    span = max(yn - yf, 1e-6)
    pool: list[list] = []  # [frame, reason, (hx, hy), conf, value=image-y]
    for i, g in enumerate(ef):
        if g in exclude:
            continue
        rows = cands.get(g) or []
        if not rows:
            continue
        # POSITION-based on the TEACHER'S ball only — AutoCam is reliable near/mid, so
        # its near-touchline balls are real close balls (verified). A no-teacher frame
        # in the near band is almost always a near PLAYER, not a ball, so skip it.
        lab = labels.get(i)
        if lab is None or int(lab[0]) < 0:
            continue
        j = int(lab[0])
        if j >= len(rows):
            continue
        x, y = float(rows[j][0]), float(rows[j][1])
        if (y - yf) / span < near_depth:  # not close to the near touchline
            continue
        scores = [float(r[2]) for r in rows]
        rank = sum(1 for s in scores if s > scores[j])
        reason = "near_misrank" if rank > 0 else "near_close"
        pool.append(
            [g, reason, (x, y), scores[j], y]
        )  # value = image-y (frontmost first)
    chosen = _spread_bins(pool, target)
    return [
        {
            "frame_idx": int(f),
            "file": f"f{int(f):06d}.jpg",
            "hint_x": round(hx, 1),
            "hint_y": round(hy, 1),
            "autocam": reason != "near_unknown",  # teacher-backed hint
            "hint_conf": round(conf, 3),
            "reason": reason,
        }
        for (f, reason, (hx, hy), conf, _v) in chosen
    ]


def _expand_near_spans(
    frames: list[dict],
    ef: list[int],
    cands: dict[int, list],
    *,
    span: int,
) -> list[dict]:
    """Expand each near ANCHOR into ``span`` consecutive dump frames (the anchor +
    the following span-1), so the near ball is labeled across the kick/arc (rising,
    blurred, peak) rather than at one instant. Each added frame gets its own hint
    from its dump candidates. Deduped by frame_idx; span<=1 is a no-op."""
    if span <= 1:
        return frames
    efidx = {g: i for i, g in enumerate(ef)}
    out: list[dict] = []
    seen: set[int] = set()
    for e in frames:
        i0 = efidx.get(int(e["frame_idx"]))
        if i0 is None:
            if int(e["frame_idx"]) not in seen:
                seen.add(int(e["frame_idx"]))
                out.append(e)
            continue
        prev = (
            float(e["hint_x"]),
            float(e["hint_y"]),
        )  # track the ball across the span
        for k in range(span):
            i = i0 + k
            if i >= len(ef):
                break
            g = int(ef[i])
            if g in seen:
                continue
            seen.add(g)
            if k == 0:
                out.append(e)  # keep the anchor's own hint/reason
                continue
            # seed the neighbour hint by CONTINUITY — the candidate nearest the
            # ball's last position — not the top near-candidate (which jumps to a
            # distractor). Keeps the seed on the arc so the frame is worth labeling.
            rows = cands.get(g) or []
            if rows:
                xy = np.asarray([(r[0], r[1]) for r in rows], float)
                j = int(np.argmin(np.linalg.norm(xy - np.asarray(prev), axis=1)))
                hx, hy, conf = (
                    float(rows[j][0]),
                    float(rows[j][1]),
                    float(rows[j][2]),
                )
            else:
                hx, hy, conf = prev[0], prev[1], 0.0
            prev = (hx, hy)
            out.append(
                {
                    "frame_idx": g,
                    "file": f"f{g:06d}.jpg",
                    "hint_x": round(hx, 1),
                    "hint_y": round(hy, 1),
                    "autocam": False,
                    "hint_conf": round(conf, 3),
                    "reason": "near_span",
                }
            )
    return out


def select_kick_frames(
    ef: list[int],
    geom,
    sel: dict[int, tuple[float, float]],
    gaps: list[int],
    *,
    lead: int,
    trail: int,
    min_exit_mpf: float,
    min_miss: int,
    max_miss: int,
    target: int,
) -> list[dict]:
    """Kick/loss-event miner (Mark 2026-07-10): close-ball ARCS the detector drops.
    A ball tracked on the ground, struck (fast in-field exit), then lost for the
    flight (``min_miss..max_miss`` missed frames) — these are the airborne balls the
    geometric size miner is blind to (they're not even in the candidate dump). Serve
    a WIDE bracketed window per event: ``lead`` frames of lead-up (ball visible, its
    direction readable) -> the flight -> the landing -> ``trail`` frames. Full-frame,
    NO seed on the airborne frames (we genuinely don't know where the ball is — that
    IS the failure); ground frames carry the track's own position as a soft seed.
    ``target`` caps the number of EVENTS (each expands to a window)."""
    present = set(sel)
    n = len(ef)
    cum = [0] * n
    for t in range(1, n):
        cum[t] = cum[t - 1] + gaps[t]

    def wpos(i: int) -> np.ndarray:
        return geom.image_to_world(np.asarray([sel[i]], float))[0]

    events: list[list] = []  # [exit_speed, i, j]
    for i in sorted(present):
        if (i - 1) not in present or (i + 1) in present:
            continue
        j = i + 1
        while j < n and j not in present:
            j += 1
        nmiss = j - i - 1
        if j >= n or not (min_miss <= nmiss <= max_miss):
            continue
        exit_spd = float(np.linalg.norm(wpos(i) - wpos(i - 1))) / max(
            1, cum[i] - cum[i - 1]
        )
        if exit_spd >= min_exit_mpf:
            events.append([exit_spd, i, j])
    events.sort(reverse=True)  # strongest kicks first, then cap
    events = sorted(events[:target], key=lambda e: e[1])
    out: list[dict] = []
    seen: set[int] = set()
    for _spd, i, j in events:
        for k in range(max(0, i - lead), min(n - 1, j + trail) + 1):
            g = int(ef[k])
            if g in seen:
                continue
            seen.add(g)
            phase = (
                "kick_lead"
                if k < i
                else "kick_launch"
                if k == i
                else "kick_flight"
                if k < j
                else "kick_land"
                if k == j
                else "kick_trail"
            )
            hx, hy = sel[k] if k in present else sel[i]  # gap frames: neutral anchor
            out.append(
                {
                    "frame_idx": g,
                    "file": f"f{g:06d}.jpg",
                    "hint_x": round(float(hx), 1),
                    "hint_y": round(float(hy), 1),
                    "autocam": False,
                    "hint_conf": 0.0,
                    "reason": phase,
                }
            )
    return sorted(out, key=lambda e: e["frame_idx"])


def select_diverge_frames(
    ef: list[int],
    geom,
    sel: dict[int, tuple[float, float]],
    track: dict[int, tuple[float, float]],
    human_balls: dict[int, tuple[float, float]],
    *,
    stride: int,
    disagree_m: float = 10.0,
    teleport_mpf: float = 2.5,
    miss_run_min: int = 5,
    target: int = 120,
    ctx: tuple[int, ...] = (-2, -1, 1, 2),
) -> list[dict]:
    """Track-audit anchors + context frames (see module docstring, ``diverge``).

    ``sel``/``track`` are :func:`~training.world_model.reranker.rerank` /
    ``kalman_smooth`` outputs keyed by ef INDEX. Anchor value ranks disagreements
    over teleports over miss runs; each anchor expands with ``ctx`` dump-step
    offsets so the annotator sees the ball's motion around the flagged moment.
    """

    def w(p):
        return geom.image_to_world(np.asarray([p], float))[0]

    anchors: list[list] = []  # [g, reason, i, value]
    ef_arr = np.asarray(ef, int)
    # (a) human label vs smoothed track
    for g_h, xy in human_balls.items():
        k = int(np.searchsorted(ef_arr, g_h))
        best, bd = None, stride // 2 + 1
        for j in (k - 1, k):
            if 0 <= j < len(ef_arr) and abs(int(ef_arr[j]) - g_h) < bd:
                best, bd = j, abs(int(ef_arr[j]) - g_h)
        if best is None or best not in track:
            continue
        d = float(np.linalg.norm(w(track[best]) - w(xy)))
        if d > disagree_m:
            anchors.append([ef[best], "diverge", best, 2000.0 + d])
    # (b) teleports in the RAW selection (identity switches the smoother hides)
    sk = sorted(sel)
    for a, b in zip(sk, sk[1:], strict=False):
        df = ef[b] - ef[a]
        if df <= 3 * stride:
            rate = float(np.linalg.norm(w(sel[b]) - w(sel[a]))) / max(df, 1)
            if rate > teleport_mpf:
                anchors.append([ef[b], "teleport", b, 1000.0 + rate])
    # (c) sustained miss runs (low-confidence stretches -> fill-in candidates)
    missing = [i for i in range(len(ef)) if i not in sel]
    run: list[int] = []
    for i in [*missing, -10]:  # sentinel flushes the last run
        if run and i != run[-1] + 1:
            if len(run) >= miss_run_min:
                mid = run[len(run) // 2]
                anchors.append([ef[mid], "trackmiss", mid, float(len(run))])
            run = []
        run.append(i)
    n_anchor = max(1, target // (len(ctx) + 1))
    # per-signal quotas (a label-rich game would otherwise fill every slot with
    # disagreements and starve the teleport/miss-run audit)
    quotas = {"diverge": n_anchor // 2, "teleport": n_anchor // 4}
    quotas["trackmiss"] = n_anchor - sum(quotas.values())
    chosen: list[list] = []
    leftover = 0
    for reason in ("diverge", "teleport", "trackmiss"):
        rows = [a for a in anchors if a[1] == reason]
        got = _spread_bins(rows, quotas[reason] + leftover)
        leftover = quotas[reason] + leftover - len(got)
        chosen.extend(got)
    # expand with context; anchors win on collision
    out: dict[int, dict] = {}
    for g, reason, i, _v in chosen:
        for off in (0, *ctx):
            j = i + off
            if not (0 <= j < len(ef)) or (ef[j] in out and off != 0):
                continue
            hx, hy = track.get(j, (3840.0, 1080.0))
            out[ef[j]] = {
                "frame_idx": int(ef[j]),
                "file": f"f{int(ef[j]):06d}.jpg",
                "hint_x": round(float(hx), 1),
                "hint_y": round(float(hy), 1),
                "autocam": False,
                "hint_conf": 0.0,
                "reason": reason if off == 0 else f"{reason}_ctx",
            }
    return [out[g] for g in sorted(out)]


def select_span_frames(
    windows: list[tuple[int, int]],
    dets: dict[int, list],
    total_frames: int,
    *,
    stride: int = 8,
) -> list[dict]:
    """Contiguous stride-aligned frames over explicit windows; hint = AutoCam's top
    detection when one exists that frame (a head start, not ground truth)."""
    out: list[dict] = []
    for lo, hi in windows:
        start = ((max(lo, 0) + stride - 1) // stride) * stride
        for g in range(start, min(hi, total_frames or hi), stride):
            top = max(dets.get(g, []), key=lambda c: float(c[2]), default=None)
            out.append(
                {
                    "frame_idx": int(g),
                    "file": f"f{g:06d}.jpg",
                    "hint_x": round(float(top[0]), 1) if top else 3840.0,
                    "hint_y": round(float(top[1]), 1) if top else 1080.0,
                    "autocam": top is not None,
                    "hint_conf": round(float(top[2]), 3) if top else 0.0,
                    "reason": "span",
                }
            )
    return out


def select_disagree_frames(
    bench: dict[int, dict],
    plan: list,
    g_start: int,
    *,
    target: int,
    hw: float = 1200.0,
    hh: float = 500.0,
) -> list[dict]:
    """Tier-B rows where the planned camera does NOT contain AutoCam's claimed ball,
    grouped into runs (one midpoint anchor per run, value = run length)."""
    events: list[tuple[int, dict]] = []
    for g, r in sorted(bench.items()):
        if r.get("tier") != "autocam":
            continue
        i = g - g_start
        if not (0 <= i < len(plan)):
            continue
        cx, cy = plan[i][0], plan[i][1]
        if ((r["x"] - cx) / hw) ** 2 + ((r["y"] - cy) / hh) ** 2 > 1.0:
            events.append((g, r))
    runs: list[list[tuple[int, dict]]] = []
    for g, r in events:
        if runs and g - runs[-1][-1][0] <= 48:
            runs[-1].append((g, r))
        else:
            runs.append([(g, r)])
    pool = []
    for run in runs:
        mid_g, mid_r = run[len(run) // 2]
        pool.append([mid_g, mid_r, float(len(run))])
    chosen = _spread_bins(pool, target)
    # flank every anchor with +-ctx frames so the annotator can read the ball's
    # MOTION (Mark: a single out-of-context frame is often humanly unresolvable)
    out: dict[int, dict] = {}
    for g, r, _v in chosen:
        for off, reason in ((-8, "disagree_ctx"), (0, "disagree"), (8, "disagree_ctx")):
            gg = int(g) + off
            if gg in out and off != 0:
                continue
            out[gg] = {
                "frame_idx": gg,
                "file": f"f{gg:06d}.jpg",
                "hint_x": round(float(r["x"]), 1),
                "hint_y": round(float(r["y"]), 1),
                "autocam": True,  # the hint IS AutoCam's claim (at the anchor)
                "hint_conf": 0.0,
                "reason": reason,
            }
    return [out[g] for g in sorted(out)]


def select_frames(
    dets: dict[int, list],
    poly_np: np.ndarray,
    in_active,
    *,
    criteria: str,
    lc_lo: float,
    lc_hi: float,
    dist_ratio: float,
    target: int,
    exclude: set[int],
    margin: float,
) -> list[dict]:
    """Return the chosen frames as manifest ``frames[]`` dicts, temporally spread.

    ``dets``: ``{global_frame: [(x, y, conf), ...]}`` (any order). ``in_active(f)`` -> bool.
    """
    pool: list[list] = []  # [frame, reason, (hx, hy), conf, score, autocam]
    for f in sorted(dets):
        if f in exclude or not in_active(f):
            continue
        onfield = [c for c in dets[f] if _in_field(poly_np, c[0], c[1], margin)]
        if not onfield:
            if criteria in ("lost", "hard"):
                # ball lost this frame — the human finds it (or marks occluded/out); center hint.
                pool.append([f, "lost", (3840.0, 1080.0), 0.0, 1.0, False])
            continue
        onfield.sort(key=lambda c: -float(c[2]))
        top = onfield[0]
        conf = float(top[2])
        reason, score = None, 0.0
        if criteria in ("lowconf", "hard") and lc_lo <= conf < lc_hi:
            reason, score = "lowconf_visible", (lc_hi - conf)
        elif (
            criteria in ("distractor", "hard")
            and len(onfield) >= 2
            and float(onfield[1][2]) >= dist_ratio * conf
            and conf >= lc_hi
        ):
            reason, score = "distractor", float(onfield[1][2]) / max(conf, 1e-6)
        if reason:
            pool.append([f, reason, (float(top[0]), float(top[1])), conf, score, True])
    # value is the LAST column for _spread_bins
    chosen = _spread_bins(
        [[f, r, xy, c, ac, s] for (f, r, xy, c, s, ac) in pool], target
    )
    return [
        {
            "frame_idx": int(f),
            "file": f"f{int(f):06d}.jpg",
            "hint_x": round(hx, 1),
            "hint_y": round(hy, 1),
            "autocam": bool(ac),
            "hint_conf": round(conf, 3),
            "reason": reason,
        }
        for (f, reason, (hx, hy), conf, ac, _score) in chosen
    ]


def _load_exclusions(out_dir: Path, sets: list[str]) -> set[int]:
    excl: set[int] = set()
    for s in sets:
        m = out_dir / s / "manifest.json"
        if m.exists():
            try:
                excl |= {
                    int(e["frame_idx"])
                    for e in json.loads(m.read_text()).get("frames", [])
                }
            except Exception:  # noqa: BLE001
                pass
    return excl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", required=True)
    ap.add_argument("--out", default="D:/training_data/far_label")
    ap.add_argument("--set-name", default=None, help="default: <game_id>__<criteria>")
    ap.add_argument(
        "--criteria",
        choices=[
            "hard",
            "lowconf",
            "lost",
            "distractor",
            "near",
            "kick",
            "diverge",
            "spans",
            "disagree",
        ],
        default="hard",
    )
    ap.add_argument("--campath", default=None, help="camera_path/1 artifact (disagree)")
    ap.add_argument(
        "--windows",
        default=None,
        help="spans mode: comma-separated global-frame windows 'lo-hi,lo-hi,...'",
    )
    ap.add_argument("--lc-lo", type=float, default=0.05)
    ap.add_argument("--lc-hi", type=float, default=0.25)
    ap.add_argument("--dist-ratio", type=float, default=0.7)
    ap.add_argument("--max-frames", type=int, default=160)
    ap.add_argument(
        "--margin", type=float, default=-40.0, help="in-field test margin (px)"
    )
    ap.add_argument("--exclude-sets", nargs="*", default=[])
    ap.add_argument("--no-hwaccel", action="store_true")
    ap.add_argument(
        "--analyze", action="store_true", help="print stats, write nothing, no decode"
    )
    # near/diverge-mode inputs (marathon artifact + its teacher-snap supervision)
    ap.add_argument("--fullgame-dir", default=None)
    ap.add_argument("--sel-labels", default=None)
    ap.add_argument(
        "--near-depth",
        type=float,
        default=0.55,
        help="close band = ball depth >= this (1 = near touchline); position, not size",
    )
    ap.add_argument(
        "--near-span",
        type=int,
        default=1,
        help="near mode: expand each anchor to N consecutive dump frames (2-4 = a "
        "short arc, so the detector sees the near ball rising/blurred across the kick)",
    )
    ap.add_argument("--disagree-m", type=float, default=10.0)
    ap.add_argument("--teleport-mpf", type=float, default=2.5)
    ap.add_argument("--miss-run-min", type=int, default=5)
    # kick/loss-event mining (close-ball arcs): a fast in-field exit + a detection
    # gap, bracketed WIDE (lead-up -> flight -> landing), full-frame no-seed.
    ap.add_argument(
        "--kick-lead", type=int, default=10, help="dump frames before launch"
    )
    ap.add_argument(
        "--kick-trail", type=int, default=10, help="dump frames after landing"
    )
    ap.add_argument(
        "--kick-min-exit", type=float, default=1.2, help="min exit speed m/frame"
    )
    ap.add_argument(
        "--kick-min-miss", type=int, default=3, help="min flight gap (missed frames)"
    )
    ap.add_argument(
        "--kick-max-miss", type=int, default=24, help="max flight gap (missed frames)"
    )
    ap.add_argument(
        "--priority", type=int, default=None, help="landing-page sort order (1 = top)"
    )
    args = ap.parse_args()

    vdir = Path(args.game_dir)
    gj = json.loads((vdir / "game.json").read_text(encoding="utf-8", errors="ignore"))
    gid = gj["game_id"]
    poly = gj["field_polygon"]
    poly_np = np.array(poly, np.float32)
    offs = dd.seg_offsets(gj["segments"])
    dets = (
        dd.load_detections(vdir / "autocam_detections.jsonl", offs)
        if (vdir / "autocam_detections.jsonl").exists()
        else {}
    )  # held-out Irondequoit has no AutoCam artifacts; spans fall back to dump hints

    ranges = dd.active_play_ranges(gj["segments"], gj.get("game_state"))
    if ranges:

        def in_active(f: int) -> bool:
            return any(lo <= f < hi for lo, hi in ranges)
    else:
        print("[warn] no game_state active-play ranges — using ALL frames", flush=True)

        def in_active(f: int) -> bool:  # noqa: ARG001
            return True

    excl = _load_exclusions(Path(args.out), args.exclude_sets)
    near_cands: dict[int, list] = {}
    if args.criteria in ("near", "kick", "diverge", "disagree") or (
        args.criteria == "spans" and args.fullgame_dir
    ):
        if not args.fullgame_dir:
            raise SystemExit(f"--criteria {args.criteria} needs --fullgame-dir")
        from training.cli.build_selector_labels import load_fullgame_candidates
        from training.world_model.geometry import build_field_geometry

        ef, near_cands, fg_meta = load_fullgame_candidates(Path(args.fullgame_dir))
        geom = build_field_geometry(np.asarray(poly, float))
        if not geom.valid:
            raise SystemExit("field polygon does not fit a valid homography")
        hb, hn = (
            dd.load_human_labels(vdir / "ball_labels.jsonl", offs)
            if (vdir / "ball_labels.jsonl").exists()
            else ({}, set())
        )
    if args.criteria == "near":
        if not args.sel_labels:
            raise SystemExit("--criteria near needs --sel-labels")
        sel = json.loads(Path(args.sel_labels).read_text(encoding="utf-8"))["labels"]
        labels = {int(k): (int(v[0]), float(v[1])) for k, v in sel.items()}
        # never re-ask for a frame a human already labeled (any set grid, ±4)
        for g in list(hb) + list(hn):
            excl.update(range(g - 4, g + 5))
        span = max(1, args.near_span)
        frames = select_near_frames(
            ef,
            near_cands,
            labels,
            geom,
            near_depth=args.near_depth,
            target=max(1, args.max_frames // span),
            exclude=excl,
        )
        frames = _expand_near_spans(frames, ef, near_cands, span=span)
    elif args.criteria == "disagree":
        if not args.campath:
            raise SystemExit("--criteria disagree needs --campath (+ --fullgame-dir)")
        art = json.loads(Path(args.campath).read_text(encoding="utf-8"))
        bench_p = vdir / "viewport_benchmark.jsonl"
        if not bench_p.exists():
            raise SystemExit("no viewport_benchmark.jsonl for this game")
        bench: dict[int, dict] = {}
        for _ln in bench_p.read_text(encoding="utf-8").splitlines():
            if _ln.strip():
                _r = json.loads(_ln)
                if _r.get("tier") == "autocam":
                    bench[int(_r["g"])] = _r
        frames = select_disagree_frames(
            bench, art["frames"], int(art["g_start"]), target=args.max_frames
        )
        # our camera center joins the overlay so the annotator sees BOTH claims
        for e in frames:
            i = int(e["frame_idx"]) - int(art["g_start"])
            cx, cy = art["frames"][i][0], art["frames"][i][1]
            e["our_cam"] = [round(float(cx), 1), round(float(cy), 1)]
    elif args.criteria == "spans":
        if not args.windows:
            raise SystemExit("--criteria spans needs --windows 'lo-hi,lo-hi,...'")
        wins = [
            (int(a), int(bb))
            for a, bb in (p.split("-", 1) for p in args.windows.split(","))
        ]
        frames = select_span_frames(wins, dets, int(gj.get("total_frames") or 0))
        # games without AutoCam detections get hints + overlays from OUR dump
        if args.fullgame_dir:
            for e in frames:
                rows = near_cands.get(int(e["frame_idx"])) or []
                if rows and not e["autocam"]:
                    e["hint_x"], e["hint_y"] = (
                        round(float(rows[0][0]), 1),
                        round(float(rows[0][1]), 1),
                    )
                    e["hint_conf"] = round(float(rows[0][2]), 3)
    elif args.criteria == "kick":
        from dataclasses import replace

        from training.world_model.reranker import RerankConfig, rerank
        from training.world_model.tbd import Candidate

        cand_frames = [
            [
                Candidate(x=x, y=y, score=s, size_px=sz)
                for (x, y, s, sz) in near_cands[g]
            ]
            for g in ef
        ]
        gaps = [1] + [ef[i] - ef[i - 1] for i in range(1, len(ef))]
        # physical transitions (phys_sigma_px>0) so the track actually MISSES on a
        # fast kick — the default loose budget never misses (the whole flight is one
        # free hop), which is why a plain rerank finds zero kick/loss events.
        kcfg = replace(
            RerankConfig(), alpha=1.0, static_w=2.0, motion_w=0.0, phys_sigma_px=5.0
        )
        sel_track = rerank(cand_frames, geom, frame_gaps=gaps, config=kcfg)
        ecap = max(1, args.max_frames // (args.kick_lead + args.kick_trail + 6))
        frames = select_kick_frames(
            ef,
            geom,
            sel_track,
            gaps,
            lead=args.kick_lead,
            trail=args.kick_trail,
            min_exit_mpf=args.kick_min_exit,
            min_miss=args.kick_min_miss,
            max_miss=args.kick_max_miss,
            target=ecap,
        )
    elif args.criteria == "diverge":
        from training.world_model.reranker import kalman_smooth, rerank
        from training.world_model.tbd import Candidate

        cand_frames = [
            [
                Candidate(x=x, y=y, score=s, size_px=sz)
                for (x, y, s, sz) in near_cands[g]
            ]
            for g in ef
        ]
        gaps = [1] + [ef[i] - ef[i - 1] for i in range(1, len(ef))]
        sel_track = rerank(cand_frames, geom, frame_gaps=gaps)
        track = kalman_smooth(sel_track, geom)
        stride_fg = int(fg_meta.get("params", {}).get("stride", 8))
        frames = select_diverge_frames(
            ef,
            geom,
            sel_track,
            track,
            hb,
            stride=stride_fg,
            disagree_m=args.disagree_m,
            teleport_mpf=args.teleport_mpf,
            miss_run_min=args.miss_run_min,
            target=args.max_frames,
        )
    else:
        frames = select_frames(
            dets,
            poly_np,
            in_active,
            criteria=args.criteria,
            lc_lo=args.lc_lo,
            lc_hi=args.lc_hi,
            dist_ratio=args.dist_ratio,
            target=args.max_frames,
            exclude=excl,
            margin=args.margin,
        )
    reasons: dict[str, int] = {}
    for e in frames:
        reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    print(
        f"[select] {gid}: {len(dets)} det-frames, exclude {len(excl)} -> "
        f"{len(frames)} chosen (criteria={args.criteria}); reasons={reasons}",
        flush=True,
    )
    if not frames:
        raise SystemExit("no frames selected")
    if args.analyze:
        return

    # --- resolve the combined video (same axis as the detections/global frames) ---
    video = gj.get("combined_video")
    if not video or not Path(video).exists():
        cands = list(vdir.glob("combined*.mp4")) or list(vdir.glob("*-raw.mp4"))
        if not cands:
            raise SystemExit(f"no video in {vdir}")
        video = str(cands[0])

    # Skip stub folders: a real game's combined video is multiple GB, but each game day tends to
    # have a tiny "house / camera-test" recording in a sibling folder (tens of MB) that otherwise
    # becomes a garbage all-gap label set. Don't build a set from non-game footage.
    MIN_GAME_BYTES = 500 * 1024 * 1024
    vbytes = Path(video).stat().st_size
    if vbytes < MIN_GAME_BYTES:
        print(
            f"SKIP {vdir.name}: video {vbytes // (1024 * 1024)}MB < 500MB — "
            "stub/non-game footage, not building a label set",
            flush=True,
        )
        return

    set_name = args.set_name or f"{gid}__{args.criteria}"
    out = Path(args.out) / set_name
    strips = out / "strips"
    strips.mkdir(parents=True, exist_ok=True)
    for old in [*strips.glob("*.jpg"), *strips.glob("*.png")]:
        old.unlink()

    import cv2

    from training.data_prep.segment_decode import iter_frames_from_segments
    from training.data_prep.warped_dataset import resolve_video_rotation

    seg0 = gj["segments"][0]
    sw, sh = int(seg0["w"]), int(seg0["h"])
    vrot = resolve_video_rotation(video, gj.get("video_rotation"))

    # STREAM the wanted global frames from the RAW per-segment clips, NOT the re-encoded/VFR/
    # corruption-prone combined video. The combined is a stream-copy concat, so raw-segment frame f
    # is bit-identical to combined global (offset+f); decoding raw is frame-exact, corruption-isolated
    # (a bad segment loses only its own frames), and fast (a keyframe seek + short decode per label).
    # Each JPEG is written as its frame arrives — never hold the full-res frame set in memory.
    want = {int(e["frame_idx"]) for e in frames}
    written = 0
    for f, img in iter_frames_from_segments(
        vdir, gj["segments"], want, vrot, hwaccel=not args.no_hwaccel
    ):
        # lossless PNG on disk; manifest keeps .jpg names — the deployed server serves
        # the .png when present (same layout the PNG regen left on every existing set)
        cv2.imwrite(str(strips / f"f{f:06d}.png"), img)
        written += 1
    print(
        f"  wrote {written}/{len(want)} strips via raw-segment decode "
        f"({len(want) - written} unavailable — corrupt/missing segments)",
        flush=True,
    )
    if written == 0:
        raise SystemExit(
            f"no decodable strips for {gid} (video corrupt from the start)"
        )

    kept = [e for e in frames if (strips / e["file"]).with_suffix(".png").exists()]
    for e in kept:
        e.update(
            {
                "crop_x0": 0,
                "crop_y0": 0,
                "crop_w": int(sw),
                "crop_h": int(sh),
                "band": "normal",
            }
        )
    if args.criteria in ("near", "diverge", "disagree") or (
        args.criteria == "spans" and args.fullgame_dir
    ):
        # candidate overlays straight from the dump (score-sorted; top-5 render blue)
        for e in kept:
            rows = (near_cands.get(int(e["frame_idx"])) or [])[:12]
            e["context"] = [
                {
                    "x": round(float(r[0]), 1),
                    "y": round(float(r[1]), 1),
                    "df": -1 if k < 5 else 1,
                }
                for k, r in enumerate(rows)
            ]
            e["candidates"] = [
                [round(float(r[0]), 1), round(float(r[1]), 1), round(float(r[2]), 4)]
                for r in rows
            ]
            if e.get("our_cam"):
                e["context"].append(
                    {"x": e["our_cam"][0], "y": e["our_cam"][1], "df": 1}
                )
    manifest = {
        "set": set_name,
        "clip": video,
        "fps": float(gj.get("fps", 20.0)),
        "src_w": int(sw),
        "src_h": int(sh),
        "strip_y0": 0,
        "strip_y1": int(sh),
        "crop_w": int(sw),
        "full_frame": True,
        "criteria": args.criteria,
        "polygon": poly,
        "n_frames": len(kept),
        "n_autocam": sum(1 for e in kept if e["autocam"]),
        "frames": kept,
    }
    if args.priority is not None:
        manifest["priority"] = int(args.priority)
    if args.criteria in ("near", "diverge"):
        manifest["candidates_ckpt"] = fg_meta.get("ckpt", "")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        f"WROTE {out}/manifest.json: {len(kept)} frames, {manifest['n_autocam']} AutoCam-seeded "
        f"(reasons={reasons})",
        flush=True,
    )


if __name__ == "__main__":
    main()
