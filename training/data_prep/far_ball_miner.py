"""Far-ball gap miner: find detection gaps where the ball went to the far side.

The reference broadcast tracker (and our current detector) routinely lose the
ball when it travels to the FAR touchline: out there the ball is only a few
pixels wide and frequently drops below detection threshold. Those misses are
exactly the footage we need to label to train a detector that EXCEEDS the
reference on far balls — the reference can't be ground truth there because it
misses the same balls.

This module productionizes a heuristic validated on a real game: for every
detection GAP (a run of >= ``min_gap_frames`` consecutive frames with no
detection), inspect the ball's velocity over the last few detections BEFORE the
gap. If the ball was moving toward the far touchline (image y DECREASING,
``vy < 0``) AND was last seen in the far third of the field band, the ball is
almost certainly still out there during the gap — a far-ball candidate. We
extrapolate the likely far-field search region across the gap, sample frames
for the human to label, and emit a prioritized labeling queue (longer far-gaps
during active play = more valuable far-ball footage = higher priority).

Coordinate convention (matches the rest of the pipeline, see
``trajectory_gaps`` / ``trajectory_validator``):
    - y INCREASES downward in the image.
    - The FAR side of the field is the UPPER rows = LOW y.
    - Moving "toward far" therefore means ``vy < 0``.

This is ADDITIVE to the generic gap mining in ``trajectory_gaps`` /
``exp1_onnx_gaps`` (which interpolate short mid-track gaps assuming the ball
reappears nearby). Generic gap mining can't target far balls because the ball
does NOT reappear on the near side — it stays lost in the far field for the
whole gap. This miner adds the velocity-direction + far-field filter that
isolates those far-ball gaps and seeds the labeler with an extrapolated search
box per sampled frame.

Trajectory input format (format-agnostic, fully unit-testable without video):
    A trajectory is a list of per-frame records in SOURCE pixels. Each record is
    a ``FrameDetection`` (or a plain ``(frame_idx, x, y, confidence)`` tuple, or
    a dict with those keys). Missing detections are represented EITHER as an
    explicit record with ``x is None`` (a "hole") OR simply by an absent
    frame_idx (sparse trajectory). Both are supported: ``mine_far_ball_gaps``
    normalizes to a dense, gap-aware sequence using ``frame_stride`` (the spacing
    between consecutive sampled frames, e.g. 4 if every 4th source frame is
    scored). Records do NOT need to be pre-sorted.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


# A single per-frame ball record. ``x``/``y`` are SOURCE pixels; ``None`` x marks
# an explicit non-detection. A trajectory may also simply omit frames.
@dataclass
class FrameDetection:
    """One per-frame ball detection (or explicit non-detection)."""

    frame_idx: int
    x: float | None = None
    y: float | None = None
    confidence: float = 0.0

    @property
    def is_detection(self) -> bool:
        return self.x is not None and self.y is not None


# Accepted loose record shapes, normalized by ``_coerce_detection``.
RawDetection = FrameDetection | Sequence | Mapping


@dataclass
class FarBallMinerConfig:
    """Tunables for the far-ball gap miner (resolution-independent where it counts).

    Defaults reproduce the validated run: gaps >= 10 frames, velocity measured
    over the last few pre-gap detections, "far third" = upper third of the field
    band, sampling ~ every 0.5s.
    """

    # A gap must span at least this many FRAMES (source-frame units, not sampled
    # records) to be considered. Validated default: 10.
    min_gap_frames: int = 10

    # Spacing between consecutive sampled/scored frames in SOURCE-frame units.
    # The pipeline scores every Nth frame (commonly 4). Used to convert between
    # sampled records and source-frame counts, and to densify sparse input.
    frame_stride: int = 4

    # Frames per second of the SOURCE video — converts gap frames to seconds for
    # priority and reporting.
    fps: float = 25.0

    # How many pre-gap DETECTIONS to fit the velocity over (lookback window).
    velocity_lookback: int = 4

    # Velocity threshold (SOURCE pixels per SOURCE frame). The ball counts as
    # "moving toward far" when vy <= vy_toward_far_threshold (a NEGATIVE number,
    # since far = up = decreasing y). Default -1.0 px/frame.
    vy_toward_far_threshold: float = -1.0

    # Far-field cutoff expressed as a FRACTION of the field band height, measured
    # from the FAR (top) edge. 1/3 => the upper third of the band is "far field".
    # Resolution-independent: works regardless of source resolution as long as a
    # correct field_band is supplied. If ``far_field_y_abs`` is set it overrides
    # this fraction with an absolute SOURCE-pixel y cutoff.
    far_field_fraction: float = 1.0 / 3.0
    far_field_y_abs: float | None = None

    # Sampling stride for frames presented to the labeler, in SECONDS. Converted
    # to source frames via fps. Default ~ every 0.5s.
    sample_stride_seconds: float = 0.5

    # Always include at least this many sample frames per gap (so very short
    # qualifying gaps still produce a labeling target).
    min_samples_per_gap: int = 1

    # Half-size (SOURCE pixels) of the extrapolated search box seeded per sampled
    # frame. The labeler uses this as a "look roughly here" hint, not a tight box.
    search_box_half_size: float = 60.0

    # Priority is proportional to gap duration in active play, capped here so one
    # very long out-of-play stretch can't dominate the queue.
    max_priority: float = 100.0

    # Seconds of far-gap that maps to ``max_priority``. Gaps at/above this length
    # saturate the cap. Validated distribution: median ~2.5s, p90 ~17s, max ~52s,
    # so 52s saturates the queue's top by default.
    priority_saturation_seconds: float = 52.0


@dataclass
class SearchBox:
    """An axis-aligned search hint for the labeler, in SOURCE pixels."""

    frame_idx: int
    cx: float
    cy: float
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class FarBallCandidate:
    """A gap where the ball almost certainly went far but no detector fired."""

    start_frame: int  # first MISSING frame of the gap
    end_frame: int  # last MISSING frame of the gap (inclusive)
    duration_frames: int
    duration_seconds: float
    last_seen_frame: int
    last_seen_xy: tuple[float, float]
    pre_gap_velocity: tuple[float, float]  # (vx, vy) SOURCE px per SOURCE frame
    extrapolated_regions: list[SearchBox]
    sample_frame_indices: list[int]
    priority: float
    classification: str  # "far_ball" | "other"
    reason: str
    game_id: str | None = None
    segment: str | None = None
    # Field band the candidate was mined against (threaded through for the labeler).
    field_band: tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict (tuples -> lists)."""
        d = asdict(self)
        d["last_seen_xy"] = list(self.last_seen_xy)
        d["pre_gap_velocity"] = list(self.pre_gap_velocity)
        d["field_band"] = list(self.field_band)
        return d

    @classmethod
    def from_dict(cls, d: Mapping) -> FarBallCandidate:
        """Inverse of ``to_dict`` (lists -> tuples, nested SearchBox rebuilt)."""
        kwargs = dict(d)
        kwargs["last_seen_xy"] = tuple(kwargs["last_seen_xy"])
        kwargs["pre_gap_velocity"] = tuple(kwargs["pre_gap_velocity"])
        kwargs["field_band"] = tuple(kwargs.get("field_band", (0.0, 0.0)))
        kwargs["extrapolated_regions"] = [
            SearchBox(**r) if isinstance(r, Mapping) else r
            for r in kwargs.get("extrapolated_regions", [])
        ]
        return cls(**kwargs)


# Internal: a normalized detection used during mining.
@dataclass
class _Det:
    frame_idx: int
    x: float
    y: float
    confidence: float


def _coerce_detection(rec: RawDetection) -> FrameDetection:
    """Normalize a loose per-frame record into a ``FrameDetection``."""
    if isinstance(rec, FrameDetection):
        return rec
    if isinstance(rec, Mapping):
        return FrameDetection(
            frame_idx=int(rec["frame_idx"]),
            x=None if rec.get("x") is None else float(rec["x"]),
            y=None if rec.get("y") is None else float(rec["y"]),
            confidence=float(rec.get("confidence", 0.0)),
        )
    # Sequence: (frame_idx, x, y[, confidence])
    seq = list(rec)
    if len(seq) < 3:
        raise ValueError(f"detection tuple needs >= 3 fields, got {seq!r}")
    fi = int(seq[0])
    x = None if seq[1] is None else float(seq[1])
    y = None if seq[2] is None else float(seq[2])
    conf = float(seq[3]) if len(seq) > 3 and seq[3] is not None else 0.0
    return FrameDetection(frame_idx=fi, x=x, y=y, confidence=conf)


def _far_field_y_cutoff(
    field_band: tuple[float, float], cfg: FarBallMinerConfig
) -> float:
    """SOURCE-pixel y at/below which a detection counts as 'far field'.

    Far = up = LOW y, so the cutoff is measured DOWN from the band's top edge.
    Detections with ``y <= cutoff`` are in the far field.
    """
    if cfg.far_field_y_abs is not None:
        return cfg.far_field_y_abs
    y_top, y_bottom = field_band
    band_height = y_bottom - y_top
    return y_top + cfg.far_field_fraction * band_height


def _extract_detections(trajectory: Iterable[RawDetection]) -> list[_Det]:
    """Sort, dedupe, and drop non-detection holes -> clean detection list."""
    by_frame: dict[int, _Det] = {}
    for raw in trajectory:
        d = _coerce_detection(raw)
        if not d.is_detection:
            continue
        # Keep the highest-confidence detection if a frame appears twice.
        existing = by_frame.get(d.frame_idx)
        if existing is None or d.confidence >= existing.confidence:
            by_frame[d.frame_idx] = _Det(
                frame_idx=d.frame_idx,
                x=float(d.x),
                y=float(d.y),
                confidence=d.confidence,
            )
    return [by_frame[fi] for fi in sorted(by_frame)]


def _pre_gap_velocity(
    detections: list[_Det], gap_start_index: int, lookback: int
) -> tuple[float, float]:
    """Velocity (vx, vy) in SOURCE px per SOURCE frame just before a gap.

    ``gap_start_index`` is the index in ``detections`` of the LAST detection
    before the gap. Fits over up to ``lookback`` detections ending there, using
    a simple endpoint slope normalized by the elapsed source frames (robust to
    uneven sampling and to a single missing sample inside the window).
    """
    end = detections[gap_start_index]
    start_i = max(0, gap_start_index - (lookback - 1))
    start = detections[start_i]
    dframes = end.frame_idx - start.frame_idx
    if dframes <= 0:
        return 0.0, 0.0
    vx = (end.x - start.x) / dframes
    vy = (end.y - start.y) / dframes
    return vx, vy


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _build_sample_boxes(
    last: _Det,
    velocity: tuple[float, float],
    sample_frames: list[int],
    field_band: tuple[float, float],
    frame_bounds: tuple[float, float] | None,
    cfg: FarBallMinerConfig,
) -> list[SearchBox]:
    """Linearly extrapolate the ball across the gap, clamped to the field band.

    Produces one search box per sampled frame. x is clamped to frame bounds (if
    given); y is clamped to the field band AND never allowed below the far-field
    cutoff + a margin (the ball is, by hypothesis, in the far field) nor above
    the far edge of the band.
    """
    vx, vy = velocity
    y_top, y_bottom = field_band
    far_cutoff = _far_field_y_cutoff(field_band, cfg)
    half = cfg.search_box_half_size

    boxes: list[SearchBox] = []
    for fi in sample_frames:
        dt = fi - last.frame_idx
        cx = last.x + vx * dt
        cy = last.y + vy * dt
        # Keep the search center inside the far field band: between the far edge
        # (y_top) and the far-field cutoff (we expect the ball to stay far).
        cy = _clamp(cy, y_top, far_cutoff)
        if frame_bounds is not None:
            cx = _clamp(cx, frame_bounds[0], frame_bounds[1])
        x0 = cx - half
        x1 = cx + half
        y0 = max(y_top, cy - half)
        y1 = min(y_bottom, cy + half)
        if frame_bounds is not None:
            x0 = max(frame_bounds[0], x0)
            x1 = min(frame_bounds[1], x1)
        boxes.append(
            SearchBox(
                frame_idx=fi,
                cx=round(cx, 1),
                cy=round(cy, 1),
                x0=round(x0, 1),
                y0=round(y0, 1),
                x1=round(x1, 1),
                y1=round(y1, 1),
            )
        )
    return boxes


def _sample_frames(
    start_frame: int, end_frame: int, cfg: FarBallMinerConfig
) -> list[int]:
    """Frames within [start_frame, end_frame] presented for labeling.

    Stride is ``sample_stride_seconds`` converted to source frames, but always
    aligned to ``frame_stride`` (we can only present frames the pipeline scored)
    and always yielding at least ``min_samples_per_gap`` frames.
    """
    stride = max(cfg.frame_stride, round(cfg.sample_stride_seconds * cfg.fps))
    # Align stride to a multiple of frame_stride so samples land on scored frames.
    stride = max(cfg.frame_stride, (stride // cfg.frame_stride) * cfg.frame_stride)

    samples: list[int] = []
    fi = start_frame
    while fi <= end_frame:
        samples.append(fi)
        fi += stride

    if len(samples) < cfg.min_samples_per_gap:
        # Fall back to evenly spaced frames inside the gap.
        n = cfg.min_samples_per_gap
        if n == 1:
            samples = [(start_frame + end_frame) // 2]
        else:
            span = end_frame - start_frame
            samples = [round(start_frame + span * k / (n - 1)) for k in range(n)]
    return samples


def _priority(duration_seconds: float, cfg: FarBallMinerConfig) -> float:
    """Priority proportional to far-gap duration, capped at ``max_priority``."""
    if cfg.priority_saturation_seconds <= 0:
        return cfg.max_priority
    frac = duration_seconds / cfg.priority_saturation_seconds
    return round(_clamp(frac, 0.0, 1.0) * cfg.max_priority, 2)


def mine_far_ball_gaps(
    trajectory: Iterable[RawDetection],
    field_band: tuple[float, float],
    config: FarBallMinerConfig | None = None,
    *,
    game_id: str | None = None,
    segment: str | None = None,
    frame_bounds: tuple[float, float] | None = None,
) -> list[FarBallCandidate]:
    """Mine far-ball detection gaps from a single ball trajectory.

    Args:
        trajectory: Per-frame ball records (see module docstring). Gaps may be
            explicit ``x is None`` holes or simply absent frame indices.
        field_band: (y_top, y_bottom) of the field in SOURCE pixels. ``y_top`` is
            the FAR edge (low y), ``y_bottom`` the NEAR edge. Used for the
            resolution-independent far-field threshold and box clamping.
        config: Tunables; defaults reproduce the validated run.
        game_id / segment: Threaded onto each candidate for the labeling queue.
        frame_bounds: Optional (x_min, x_max) source-pixel bounds for clamping
            extrapolated search boxes (e.g. (0, 4096)).

    Returns:
        ``FarBallCandidate`` objects (far-ball gaps only), sorted by priority
        descending. Non-qualifying gaps are dropped (not returned as "other").
    """
    cfg = config or FarBallMinerConfig()
    detections = _extract_detections(trajectory)
    if len(detections) < 2:
        return []

    far_cutoff = _far_field_y_cutoff(field_band, cfg)
    candidates: list[FarBallCandidate] = []

    for i in range(len(detections) - 1):
        last = detections[i]
        nxt = detections[i + 1]
        gap_frames = nxt.frame_idx - last.frame_idx - cfg.frame_stride

        # A "gap" is the missing span between two detections, beyond the normal
        # one-stride spacing. gap_frames is the number of source frames with no
        # detection (approx; exact when sampling is regular).
        if gap_frames < cfg.min_gap_frames:
            continue

        # --- Far-ball classification ---
        vx, vy = _pre_gap_velocity(detections, i, cfg.velocity_lookback)
        toward_far = vy <= cfg.vy_toward_far_threshold
        last_seen_far = last.y <= far_cutoff

        if not (toward_far and last_seen_far):
            # Occlusion / fast near kick / out of play — not a far-ball gap.
            continue

        start_frame = last.frame_idx + cfg.frame_stride
        end_frame = nxt.frame_idx - cfg.frame_stride
        if end_frame < start_frame:
            end_frame = start_frame
        duration_frames = end_frame - start_frame + cfg.frame_stride
        duration_seconds = duration_frames / cfg.fps

        sample_frames = _sample_frames(start_frame, end_frame, cfg)
        boxes = _build_sample_boxes(
            last, (vx, vy), sample_frames, field_band, frame_bounds, cfg
        )
        priority = _priority(duration_seconds, cfg)

        candidates.append(
            FarBallCandidate(
                start_frame=start_frame,
                end_frame=end_frame,
                duration_frames=duration_frames,
                duration_seconds=round(duration_seconds, 3),
                last_seen_frame=last.frame_idx,
                last_seen_xy=(round(last.x, 1), round(last.y, 1)),
                pre_gap_velocity=(round(vx, 4), round(vy, 4)),
                extrapolated_regions=boxes,
                sample_frame_indices=sample_frames,
                priority=priority,
                classification="far_ball",
                reason=(
                    f"pre-gap vy={vy:.2f} <= {cfg.vy_toward_far_threshold} "
                    f"(toward far) and last_seen_y={last.y:.0f} <= "
                    f"far_cutoff={far_cutoff:.0f}"
                ),
                game_id=game_id,
                segment=segment,
                field_band=(float(field_band[0]), float(field_band[1])),
            )
        )

    candidates.sort(key=lambda c: c.priority, reverse=True)
    logger.info(
        "Mined %d far-ball gap candidates (game=%s segment=%s) from %d detections",
        len(candidates),
        game_id,
        segment,
        len(detections),
    )
    return candidates


def merge_candidates(
    *candidate_lists: Sequence[FarBallCandidate],
) -> list[FarBallCandidate]:
    """Merge candidates across games/segments and sort by priority descending.

    Ties broken by longer gap first, then game_id/segment/start_frame for
    deterministic ordering.
    """
    merged: list[FarBallCandidate] = []
    for lst in candidate_lists:
        merged.extend(lst)
    merged.sort(
        key=lambda c: (
            -c.priority,
            -c.duration_frames,
            c.game_id or "",
            c.segment or "",
            c.start_frame,
        )
    )
    return merged


def candidates_to_queue(
    candidates: Sequence[FarBallCandidate],
) -> list[dict]:
    """Serialize candidates into labeling-queue rows for the annotation helper.

    Mirrors the shape consumed by ``flywheel.priority_queue`` (``game_id``,
    ``segment``, ``frame_start``, ``priority``, ``reviewed``) while adding the
    far-ball-specific seed data (sample frames + extrapolated search boxes).
    """
    rows: list[dict] = []
    for c in candidates:
        d = c.to_dict()
        d["frame_start"] = c.start_frame
        d["queue_kind"] = "far_ball"
        d["reviewed"] = False
        rows.append(d)
    return rows


def write_queue_json(candidates: Sequence[FarBallCandidate], path) -> int:
    """Write a labeling-queue JSON file. Returns the number of rows written."""
    from pathlib import Path

    rows = candidates_to_queue(candidates)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(rows, f, indent=2)
    logger.info("Wrote %d far-ball queue rows to %s", len(rows), out)
    return len(rows)


def queue_to_candidates(rows: Iterable[Mapping]) -> list[FarBallCandidate]:
    """Inverse of ``candidates_to_queue`` (round-trip for tests / re-sorting)."""
    out: list[FarBallCandidate] = []
    for row in rows:
        d = dict(row)
        d.pop("frame_start", None)
        d.pop("queue_kind", None)
        d.pop("reviewed", None)
        out.append(FarBallCandidate.from_dict(d))
    return out


__all__ = [
    "FrameDetection",
    "FarBallMinerConfig",
    "SearchBox",
    "FarBallCandidate",
    "mine_far_ball_gaps",
    "merge_candidates",
    "candidates_to_queue",
    "queue_to_candidates",
    "write_queue_json",
]
