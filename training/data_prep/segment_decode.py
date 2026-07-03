r"""Frame-exact extraction from the RAW per-segment camera clips, bypassing the combined video.

Why: the combined video is a **stream-copy concat** of the raw segments (frames are bit-identical —
verified 0.00 mean-abs-diff, including across segment boundaries) plus a realigned audio track, which
makes it VFR. Decoding it end-to-end is slow, and a corrupt packet anywhere crashes the whole decode.
The raw Reolink clips are GOP=20 and VFR. So to pull specific GLOBAL frames (where
``global = segment.global_offset + f``) we decode the raw clips directly:

  * map each wanted global to ``(segment, local f)``,
  * per segment, build the presentation-order PTS list (frame ``f`` has ``pts = pmap[f]``),
  * seek to the keyframe at/before each cluster of wanted frames and decode forward, matching frames
    by PTS.

Corruption-isolated (a bad segment only loses its own frames) and fast (decode ~one GOP per label).
``f`` is presentation order, exactly how AutoCam numbers detections, so the extracted frame is
identical to the combined video's ``global = offset + f``.

**Stream, don't accumulate.** ``iter_frames_from_segments`` yields ``(global, bgr)`` one at a time in
ascending global order, so callers processing thousands of frames (eval, distill build) never hold
more than one frame. ``extract_frames_from_segments`` is a dict convenience for *small* frame sets
only — a full 7680×2160 frame is ~50 MB, so hundreds of them will thrash / OOM a 16 GB box.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np


def iter_frames_from_segments(
    game_dir,
    segments: list[dict],
    wanted_globals,
    vrot,
    *,
    hwaccel: bool = True,
    cluster_gap: int = 30,
) -> Iterator[tuple[int, np.ndarray]]:
    """Yield ``(global_frame, bgr_ndarray)`` for ``wanted_globals`` in ascending global order, read
    from the raw per-segment clips (``<segment.seg>.mp4`` beside the combined video). A segment whose
    clip is missing or fails to decode is skipped with a message (its frames simply don't appear)."""
    import av  # noqa: PLC0415

    game_dir = Path(game_dir)
    wanted = {int(g) for g in wanted_globals}
    for s in sorted(segments, key=lambda z: int(z["global_offset"])):
        lo = int(s["global_offset"])
        hi = lo + int(s["frames"])
        local = {g - lo: g for g in wanted if lo <= g < hi}
        if not local:
            continue
        clip = game_dir / f"{s['seg']}.mp4"
        if not clip.exists():
            print(
                f"  segment {s['seg']}: raw clip missing, skipping {len(local)} frames",
                flush=True,
            )
            continue
        try:
            yield from _iter_one(av, str(clip), local, vrot, hwaccel, cluster_gap)
        except Exception as e:  # noqa: BLE001 — corrupt/undecodable segment: isolate it
            print(
                f"  segment {s['seg']}: decode error ({type(e).__name__}: {e})",
                flush=True,
            )


def _iter_one(av, clip, local, vrot, hwaccel, cluster_gap):
    from training.data_prep.warped_dataset import (  # noqa: PLC0415
        apply_display_rotation,
    )

    # presentation-order PTS: frame f (0-indexed, presentation order) has pts == pmap[f].
    c = av.open(clip)
    s = c.streams.video[0]
    pmap = sorted(p.pts for p in c.demux(s) if p.pts is not None)
    c.close()
    n = len(pmap)
    pts_to_f = {p: i for i, p in enumerate(pmap)}

    _hw = None
    if hwaccel:
        try:
            _hw = av.codec.hwaccel.HWAccel(
                device_type="cuda", allow_software_fallback=True
            )
        except Exception:  # noqa: BLE001
            _hw = None
    c = av.open(clip, hwaccel=_hw) if _hw else av.open(clip)
    s = c.streams.video[0]
    if _hw is None:
        s.thread_type = "AUTO"

    wanted_f = sorted(f for f in local if 0 <= f < n)
    # cluster nearby frames so one seek + short forward-decode covers a whole band (t-2, t-1, t)
    clusters: list[list[int]] = []
    for f in wanted_f:
        if clusters and f - clusters[-1][-1] <= cluster_gap:
            clusters[-1].append(f)
        else:
            clusters.append([f])

    try:
        for cluster in clusters:
            want_pts = {pmap[f] for f in cluster}
            target_hi = pmap[cluster[-1]]
            c.seek(pmap[cluster[0]], stream=s, backward=True, any_frame=False)
            for fr in c.decode(s):
                if fr.pts is None:
                    continue
                if fr.pts in want_pts:
                    yield (
                        local[pts_to_f[fr.pts]],
                        apply_display_rotation(fr.to_ndarray(format="bgr24"), vrot),
                    )
                if fr.pts >= target_hi:
                    break
    finally:
        c.close()


def extract_frames_from_segments(
    game_dir,
    segments: list[dict],
    wanted_globals,
    vrot,
    *,
    hwaccel: bool = True,
    cluster_gap: int = 30,
) -> dict[int, np.ndarray]:
    """Dict form — holds every requested frame in memory. Use only for SMALL frame sets; for anything
    large, stream with ``iter_frames_from_segments`` instead (a 7680×2160 frame is ~50 MB)."""
    return dict(
        iter_frames_from_segments(
            game_dir,
            segments,
            wanted_globals,
            vrot,
            hwaccel=hwaccel,
            cluster_gap=cluster_gap,
        )
    )
