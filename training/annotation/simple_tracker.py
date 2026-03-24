"""Simple ball tracker using constant-velocity prediction.

A lightweight tracker that doesn't require numpy/filterpy.
Uses constant-velocity model with gated nearest-neighbor association.
Fills gaps with linear interpolation.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SimpleDetection:
    x: float
    y: float
    frame_idx: int


@dataclass
class SimpleTrack:
    track_id: int
    detections: list[SimpleDetection] = field(default_factory=list)
    missing_frames: int = 0
    active: bool = True

    # Current estimated state
    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0

    @property
    def length(self) -> int:
        return len(self.detections)

    def predict(self, dt: float = 1.0):
        """Predict next position using constant velocity."""
        self.x += self.vx * dt
        self.y += self.vy * dt

    def update(self, det: SimpleDetection):
        """Update track with a new detection."""
        if self.detections:
            prev = self.detections[-1]
            dt = det.frame_idx - prev.frame_idx
            if dt > 0:
                # Exponential smoothing on velocity (alpha=0.5)
                new_vx = (det.x - prev.x) / dt
                new_vy = (det.y - prev.y) / dt
                self.vx = 0.5 * self.vx + 0.5 * new_vx
                self.vy = 0.5 * self.vy + 0.5 * new_vy

        self.x = det.x
        self.y = det.y
        self.detections.append(det)
        self.missing_frames = 0


class SimpleTracker:
    """Lightweight ball tracker with gap interpolation.

    - Constant-velocity prediction during occlusion
    - Gated nearest-neighbor association
    - Linear interpolation to fill gaps
    - Picks single best track (longest with most movement)
    """

    def __init__(
        self,
        gate_distance: float = 200.0,
        max_missing: int = 15,
        min_track_length: int = 3,
    ):
        self.gate_distance = gate_distance
        self.max_missing = max_missing
        self.min_track_length = min_track_length
        self.tracks: list[SimpleTrack] = []
        self._next_id = 0

    def update(self, frame_idx: int, detections: list[tuple[float, float, float]]):
        """Process detections for one frame.

        Args:
            frame_idx: Frame index
            detections: List of (x, y, confidence) in panoramic coords
        """
        dets = [SimpleDetection(x=x, y=y, frame_idx=frame_idx) for x, y, _ in detections]

        # Predict all active tracks
        for track in self.tracks:
            if track.active:
                track.predict()

        # Associate detections to tracks (greedy nearest neighbor)
        costs = []
        for ti, track in enumerate(self.tracks):
            if not track.active:
                continue
            for di, det in enumerate(dets):
                dist = ((det.x - track.x) ** 2 + (det.y - track.y) ** 2) ** 0.5
                if dist < self.gate_distance:
                    costs.append((dist, ti, di))

        costs.sort()
        used_tracks = set()
        used_dets = set()

        for dist, ti, di in costs:
            if ti in used_tracks or di in used_dets:
                continue
            self.tracks[ti].update(dets[di])
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

        # Start new tracks from unmatched detections
        for di, det in enumerate(dets):
            if di not in used_dets:
                track = SimpleTrack(
                    track_id=self._next_id,
                    detections=[det],
                    x=det.x,
                    y=det.y,
                )
                self._next_id += 1
                self.tracks.append(track)

    def get_tracks(self, min_length: int | None = None) -> list[SimpleTrack]:
        if min_length is None:
            min_length = self.min_track_length
        return [t for t in self.tracks if t.length >= min_length]

    def get_best_track(self, min_movement: float = 50.0) -> SimpleTrack | None:
        """Get the single most likely game ball track.

        Rejects static tracks (sideline balls, equipment) and scores
        remaining tracks by total path length — the game ball covers
        the most ground over time.

        Args:
            min_movement: Minimum max displacement to consider a track
                as potentially the game ball. Static tracks below this
                are rejected regardless of length.
        """
        valid = self.get_tracks()
        if not valid:
            return None

        def score(t: SimpleTrack) -> float:
            if len(t.detections) < 2:
                return 0

            # Compute average per-frame displacement (velocity proxy)
            total_path = 0.0
            for i in range(1, len(t.detections)):
                dx = t.detections[i].x - t.detections[i - 1].x
                dy = t.detections[i].y - t.detections[i - 1].y
                total_path += (dx**2 + dy**2) ** 0.5

            avg_step = total_path / len(t.detections)

            # Hard reject slow/static tracks (sideline balls jitter ~4px/frame)
            # Game ball averages 15+ px/frame when in play
            if avg_step < 8:
                return 0

            # Score: total_path * avg_velocity
            # This favors tracks that are both long AND fast-moving
            # A game ball track with 25 dets at 65px/f scores higher than
            # a sideline wobble with 134 dets at 17px/f
            return total_path * avg_step

        best = max(valid, key=score)
        return best if score(best) > 0 else None

    def get_trajectory(
        self, track: SimpleTrack, frame_interval: int = 8
    ) -> list[tuple[int, float, float, float]]:
        """Get full trajectory with interpolated gap positions.

        Returns list of (frame_idx, x, y, confidence) where:
        - confidence=1.0 for real detections
        - confidence=0.0 for interpolated/predicted positions
        """
        if not track.detections:
            return []

        # Build detection lookup
        det_by_frame = {d.frame_idx: d for d in track.detections}
        first = track.detections[0].frame_idx
        last = track.detections[-1].frame_idx

        trajectory = []
        for fi in range(first, last + 1, frame_interval):
            if fi in det_by_frame:
                d = det_by_frame[fi]
                trajectory.append((fi, d.x, d.y, 1.0))
            else:
                # Linear interpolation between nearest known positions
                prev_det = None
                next_det = None
                for d in track.detections:
                    if d.frame_idx <= fi:
                        prev_det = d
                    if d.frame_idx >= fi and next_det is None:
                        next_det = d

                if prev_det and next_det and prev_det.frame_idx != next_det.frame_idx:
                    t = (fi - prev_det.frame_idx) / (next_det.frame_idx - prev_det.frame_idx)
                    ix = prev_det.x + t * (next_det.x - prev_det.x)
                    iy = prev_det.y + t * (next_det.y - prev_det.y)
                    trajectory.append((fi, ix, iy, 0.0))
                elif prev_det:
                    # Extrapolate forward using last known velocity
                    dt = fi - prev_det.frame_idx
                    trajectory.append((fi, prev_det.x + track.vx * dt, prev_det.y + track.vy * dt, 0.0))

        return trajectory
