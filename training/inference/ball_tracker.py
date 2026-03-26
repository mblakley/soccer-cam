"""Kalman filter ball tracker for linking detections into trajectories.

Takes per-frame detections from the panoramic detector and produces
smooth ball trajectories with occlusion prediction.

Uses an Extended Kalman Filter with state [x, y, vx, vy, ax, ay]
in panoramic pixel coordinates.
"""

import logging
from dataclasses import dataclass, field

import numpy as np
from filterpy.kalman import KalmanFilter

logger = logging.getLogger(__name__)

# Tracker configuration
MAX_MISSING_FRAMES = 15  # Kill track after this many frames without detection
GATE_DISTANCE = 200  # Max Mahalanobis-like distance for association (pixels)
MIN_TRACK_LENGTH = 3  # Minimum detections to consider a valid track
PROCESS_NOISE_POS = 5.0  # Position process noise
PROCESS_NOISE_VEL = 10.0  # Velocity process noise
PROCESS_NOISE_ACC = 20.0  # Acceleration process noise
MEASUREMENT_NOISE = 15.0  # Measurement noise (pixels)


@dataclass
class Detection:
    """A single ball detection in panoramic coordinates."""

    x: float
    y: float
    confidence: float
    frame_idx: int


@dataclass
class Track:
    """A tracked ball trajectory."""

    track_id: int
    detections: list[Detection] = field(default_factory=list)
    predictions: list[tuple[int, float, float]] = field(
        default_factory=list
    )  # (frame, x, y)
    kf: KalmanFilter | None = field(default=None, repr=False)
    missing_frames: int = 0
    active: bool = True

    @property
    def length(self) -> int:
        return len(self.detections)

    @property
    def last_position(self) -> tuple[float, float]:
        if self.kf is not None:
            return float(self.kf.x[0]), float(self.kf.x[1])
        if self.detections:
            d = self.detections[-1]
            return d.x, d.y
        return 0.0, 0.0


def _create_kf(det: Detection, dt: float = 1.0) -> KalmanFilter:
    """Create a Kalman filter initialized from a detection.

    State vector: [x, y, vx, vy, ax, ay]
    Measurement: [x, y]
    """
    kf = KalmanFilter(dim_x=6, dim_z=2)

    # State transition matrix (constant acceleration model)
    kf.F = np.array(
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

    # Measurement matrix (we observe position only)
    kf.H = np.array(
        [
            [1, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0],
        ],
        dtype=np.float64,
    )

    # Process noise
    kf.Q = np.diag(
        [
            PROCESS_NOISE_POS**2,
            PROCESS_NOISE_POS**2,
            PROCESS_NOISE_VEL**2,
            PROCESS_NOISE_VEL**2,
            PROCESS_NOISE_ACC**2,
            PROCESS_NOISE_ACC**2,
        ]
    )

    # Measurement noise
    kf.R = np.diag([MEASUREMENT_NOISE**2, MEASUREMENT_NOISE**2])

    # Initial state
    kf.x = np.array([det.x, det.y, 0, 0, 0, 0], dtype=np.float64)

    # Initial covariance (high uncertainty for velocity/acceleration)
    kf.P = np.diag([50**2, 50**2, 100**2, 100**2, 200**2, 200**2])

    return kf


class BallTracker:
    """Multi-frame ball tracker using Kalman filtering.

    Usage:
        tracker = BallTracker()
        for frame_idx, detections in enumerate(all_detections):
            tracks = tracker.update(frame_idx, detections)
        final_tracks = tracker.get_tracks()
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
            kf=_create_kf(det),
        )
        self._next_id += 1
        return track

    def update(
        self,
        frame_idx: int,
        detections: list[tuple[float, float, float]],
    ) -> list[Track]:
        """Process detections for a single frame.

        Args:
            frame_idx: Current frame index
            detections: List of (x, y, confidence) in panoramic coordinates

        Returns:
            List of currently active tracks
        """
        dets = [
            Detection(x=x, y=y, confidence=conf, frame_idx=frame_idx)
            for x, y, conf in detections
        ]

        # Predict all active tracks
        for track in self.tracks:
            if track.active and track.kf is not None:
                track.kf.predict()
                px, py = float(track.kf.x[0]), float(track.kf.x[1])
                track.predictions.append((frame_idx, px, py))

        # Associate detections to tracks (greedy nearest neighbor)
        used_dets = set()
        used_tracks = set()

        # Build cost matrix
        costs = []
        for ti, track in enumerate(self.tracks):
            if not track.active:
                continue
            tx, ty = track.last_position
            for di, det in enumerate(dets):
                dist = ((det.x - tx) ** 2 + (det.y - ty) ** 2) ** 0.5
                if dist < self.gate_distance:
                    costs.append((dist, ti, di))

        # Sort by distance and greedily assign
        costs.sort()
        for dist, ti, di in costs:
            if ti in used_tracks or di in used_dets:
                continue
            # Update track with detection
            track = self.tracks[ti]
            det = dets[di]
            track.kf.update(np.array([det.x, det.y]))
            track.detections.append(det)
            track.missing_frames = 0
            used_tracks.add(ti)
            used_dets.add(di)

        # Handle unmatched tracks
        for ti, track in enumerate(self.tracks):
            if not track.active:
                continue
            if ti not in used_tracks:
                track.missing_frames += 1
                if track.missing_frames > self.max_missing:
                    track.active = False

        # Create new tracks for unmatched detections
        for di, det in enumerate(dets):
            if di not in used_dets:
                self.tracks.append(self._new_track(det))

        return [t for t in self.tracks if t.active]

    def get_tracks(self, min_length: int | None = None) -> list[Track]:
        """Get all tracks that meet minimum length requirement."""
        if min_length is None:
            min_length = self.min_track_length
        return [t for t in self.tracks if t.length >= min_length]

    def get_best_track(self) -> Track | None:
        """Get the single most likely ball track.

        Scores by: track length × average confidence.
        """
        valid = self.get_tracks()
        if not valid:
            return None

        def score(t: Track) -> float:
            avg_conf = sum(d.confidence for d in t.detections) / max(
                len(t.detections), 1
            )
            return t.length * avg_conf

        return max(valid, key=score)

    def get_trajectory(self, track: Track) -> list[tuple[int, float, float, float]]:
        """Get interpolated trajectory for a track.

        Returns list of (frame_idx, x, y, confidence) for every frame
        in the track's range, using predictions to fill gaps.
        """
        if not track.detections:
            return []

        # Build detection lookup
        det_by_frame: dict[int, Detection] = {}
        for d in track.detections:
            det_by_frame[d.frame_idx] = d

        # Build prediction lookup
        pred_by_frame: dict[int, tuple[float, float]] = {}
        for frame_idx, px, py in track.predictions:
            pred_by_frame[frame_idx] = (px, py)

        first = track.detections[0].frame_idx
        last = track.detections[-1].frame_idx

        trajectory = []
        for fi in range(first, last + 1):
            if fi in det_by_frame:
                d = det_by_frame[fi]
                trajectory.append((fi, d.x, d.y, d.confidence))
            elif fi in pred_by_frame:
                px, py = pred_by_frame[fi]
                trajectory.append((fi, px, py, 0.0))  # predicted, no confidence

        return trajectory
