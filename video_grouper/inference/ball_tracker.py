"""Kalman ball tracker — links per-frame detections into smooth trajectories.

Constant-acceleration model with state ``[x, y, vx, vy, ax, ay]`` and
position-only measurement ``[x, y]``. Implemented with plain numpy so the
runtime doesn't depend on filterpy/scipy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

MAX_MISSING_FRAMES = 15
GATE_DISTANCE = 200
MIN_TRACK_LENGTH = 3
PROCESS_NOISE_POS = 5.0
PROCESS_NOISE_VEL = 10.0
PROCESS_NOISE_ACC = 20.0
MEASUREMENT_NOISE = 15.0


@dataclass
class Detection:
    """A single ball detection in panoramic coordinates."""

    x: float
    y: float
    confidence: float
    frame_idx: int


@dataclass
class _KalmanState:
    """Mean and covariance of the constant-acceleration Kalman state."""

    x: np.ndarray  # shape (6,)
    P: np.ndarray  # shape (6, 6)


@dataclass
class Track:
    """A tracked ball trajectory."""

    track_id: int
    detections: list[Detection] = field(default_factory=list)
    predictions: list[tuple[int, float, float]] = field(default_factory=list)
    # Full per-frame Kalman state for every frame the track was active:
    # ``(frame_idx, x, y, vx, vy)``. Captured AFTER the predict+update cycle
    # so the velocity reflects the latest measurement when one was matched
    # (otherwise it's the propagated estimate). Used by downstream consumers
    # — e.g. the render stage uses vx/vy for lead-room offsets.
    states: list[tuple[int, float, float, float, float]] = field(default_factory=list)
    missing_frames: int = 0
    active: bool = True
    _state: _KalmanState | None = field(default=None, repr=False)

    @property
    def length(self) -> int:
        return len(self.detections)

    @property
    def last_position(self) -> tuple[float, float]:
        if self._state is not None:
            return float(self._state.x[0]), float(self._state.x[1])
        if self.detections:
            d = self.detections[-1]
            return d.x, d.y
        return 0.0, 0.0


def _build_matrices(
    dt: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    F = np.array(
        [
            [1, 0, dt, 0, 0.5 * dt**2, 0],
            [0, 1, 0, dt, 0, 0.5 * dt**2],
            [0, 0, 1, 0, dt, 0],
            [0, 0, 0, 1, 0, dt],
            [0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 1],
        ],
        dtype=np.float64,
    )
    H = np.array(
        [
            [1, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0],
        ],
        dtype=np.float64,
    )
    Q = np.diag(
        [
            PROCESS_NOISE_POS**2,
            PROCESS_NOISE_POS**2,
            PROCESS_NOISE_VEL**2,
            PROCESS_NOISE_VEL**2,
            PROCESS_NOISE_ACC**2,
            PROCESS_NOISE_ACC**2,
        ]
    )
    R = np.diag([MEASUREMENT_NOISE**2, MEASUREMENT_NOISE**2])
    return F, H, Q, R


_F, _H, _Q, _R = _build_matrices()
_I6 = np.eye(6)


def _initial_state(det: Detection) -> _KalmanState:
    x = np.array([det.x, det.y, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
    P = np.diag([50**2, 50**2, 100**2, 100**2, 200**2, 200**2]).astype(np.float64)
    return _KalmanState(x=x, P=P)


def _predict(state: _KalmanState) -> None:
    state.x = _F @ state.x
    state.P = _F @ state.P @ _F.T + _Q


def _update(state: _KalmanState, measurement: np.ndarray) -> None:
    innovation = measurement - _H @ state.x
    S = _H @ state.P @ _H.T + _R
    K = state.P @ _H.T @ np.linalg.inv(S)
    state.x = state.x + K @ innovation
    state.P = (_I6 - K @ _H) @ state.P


class BallTracker:
    """Multi-frame ball tracker.

    Usage::

        tracker = BallTracker()
        for frame_idx, dets in enumerate(per_frame):
            tracker.update(frame_idx, dets)
        best = tracker.get_best_track()
    """

    def __init__(
        self,
        gate_distance: float = GATE_DISTANCE,
        max_missing: int = MAX_MISSING_FRAMES,
        min_track_length: int = MIN_TRACK_LENGTH,
    ):
        self.gate_distance = gate_distance
        self.max_missing = max_missing
        self.min_track_length = min_track_length
        self.tracks: list[Track] = []
        self._next_id = 0

    def _new_track(self, det: Detection) -> Track:
        track = Track(
            track_id=self._next_id,
            detections=[det],
            _state=_initial_state(det),
        )
        self._next_id += 1
        return track

    def update(self, frame_idx: int, detections: list[Detection]) -> list[Track]:
        """Process detections for a single frame.

        Args:
            frame_idx: Current frame index.
            detections: Detections observed at this frame, in panoramic
                pixel coordinates.

        Returns:
            The list of currently active tracks.
        """
        for track in self.tracks:
            if track.active and track._state is not None:
                _predict(track._state)
                px, py = float(track._state.x[0]), float(track._state.x[1])
                track.predictions.append((frame_idx, px, py))

        used_dets: set[int] = set()
        used_tracks: set[int] = set()

        costs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(self.tracks):
            if not track.active:
                continue
            tx, ty = track.last_position
            for di, det in enumerate(detections):
                dist = ((det.x - tx) ** 2 + (det.y - ty) ** 2) ** 0.5
                if dist < self.gate_distance:
                    costs.append((dist, ti, di))

        costs.sort()
        for _dist, ti, di in costs:
            if ti in used_tracks or di in used_dets:
                continue
            track = self.tracks[ti]
            det = detections[di]
            assert track._state is not None
            _update(track._state, np.array([det.x, det.y], dtype=np.float64))
            track.detections.append(det)
            track.missing_frames = 0
            used_tracks.add(ti)
            used_dets.add(di)

        for ti, track in enumerate(self.tracks):
            if not track.active or ti in used_tracks:
                continue
            track.missing_frames += 1
            if track.missing_frames > self.max_missing:
                track.active = False

        for di, det in enumerate(detections):
            if di not in used_dets:
                self.tracks.append(self._new_track(det))

        # Capture per-frame full state for active tracks AFTER predict+update
        # so velocity reflects the latest measurement when one matched.
        for track in self.tracks:
            if not track.active or track._state is None:
                continue
            sx = track._state.x
            track.states.append(
                (frame_idx, float(sx[0]), float(sx[1]), float(sx[2]), float(sx[3]))
            )

        return [t for t in self.tracks if t.active]

    def get_tracks(self, min_length: int | None = None) -> list[Track]:
        """Return tracks that meet the minimum length requirement."""
        if min_length is None:
            min_length = self.min_track_length
        return [t for t in self.tracks if t.length >= min_length]

    def get_best_track(self) -> Track | None:
        """Return the highest-scoring track (length × average confidence)."""
        valid = self.get_tracks()
        if not valid:
            return None

        def score(t: Track) -> float:
            avg_conf = sum(d.confidence for d in t.detections) / max(
                len(t.detections), 1
            )
            return t.length * avg_conf

        return max(valid, key=score)
