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

    def get_game_ball_tracks(
        self, min_avg_step: float = 8.0
    ) -> list[SimpleTrack]:
        """Get all tracks that move fast enough to be the game ball."""
        result = []
        for t in self.get_tracks():
            if len(t.detections) < 2:
                continue
            total_path = sum(
                ((t.detections[i].x - t.detections[i - 1].x) ** 2
                 + (t.detections[i].y - t.detections[i - 1].y) ** 2) ** 0.5
                for i in range(1, len(t.detections))
            )
            if total_path / len(t.detections) >= min_avg_step:
                result.append(t)
        return result

    def stitch_game_ball(
        self,
        max_time_gap: int = 40,
        max_spatial_gap: float = 600.0,
        min_avg_step: float = 8.0,
    ) -> list[SimpleTrack]:
        """Stitch game ball track fragments into longer chains.

        After tracking, the game ball creates many short tracks (25-50 frames)
        because the detector loses it briefly (occlusion, tile boundary, etc).
        This method chains those fragments together when they're close enough
        in time and space to be the same ball.

        Args:
            max_time_gap: Maximum frame gap between end of one track and
                start of next (40 = ~5 seconds at 8-frame interval)
            max_spatial_gap: Maximum panoramic pixel distance between
                the end of one track and start of the next
            min_avg_step: Minimum average displacement per detection to
                qualify as a game ball track

        Returns:
            List of stitched tracks (may be fewer and longer than input)
        """
        candidates = self.get_game_ball_tracks(min_avg_step)
        if not candidates:
            return []

        # Sort by first detection frame
        candidates.sort(key=lambda t: t.detections[0].frame_idx)

        stitched = []
        used = [False] * len(candidates)

        for i in range(len(candidates)):
            if used[i]:
                continue

            chain = candidates[i]
            used[i] = True

            # Greedily extend the chain forward
            changed = True
            while changed:
                changed = False
                chain_end = chain.detections[-1]
                end_fi = chain_end.frame_idx
                end_x, end_y = chain_end.x, chain_end.y
                # Use velocity to predict where the ball will be
                pred_x = end_x + chain.vx * 8  # one frame step prediction
                pred_y = end_y + chain.vy * 8

                best_j = -1
                best_time_gap = float("inf")

                for j in range(len(candidates)):
                    if used[j]:
                        continue
                    cand = candidates[j]
                    cand_start = cand.detections[0]
                    start_fi = cand_start.frame_idx

                    time_gap = start_fi - end_fi

                    # Allow negative gaps (overlapping tracks from adjacent tiles)
                    # and positive gaps (ball briefly lost)
                    if time_gap > max_time_gap:
                        continue
                    # For overlapping tracks, check if they're near each other
                    # at their overlap point
                    if time_gap < -max_time_gap:
                        continue

                    if time_gap > 0:
                        # Forward gap: check predicted distance
                        steps = max(time_gap / 8, 1)
                        dx = cand_start.x - pred_x
                        dy = cand_start.y - pred_y
                        spatial_dist = (dx**2 + dy**2) ** 0.5
                        effective_max = min(max_spatial_gap, 100 * steps)
                    else:
                        # Overlapping: check distance between endpoints
                        # that are closest in time
                        dx = cand_start.x - end_x
                        dy = cand_start.y - end_y
                        spatial_dist = (dx**2 + dy**2) ** 0.5
                        effective_max = 200  # overlapping tracks should be close

                    if spatial_dist > effective_max:
                        continue

                    # Prefer smallest absolute time gap
                    abs_gap = abs(time_gap)
                    if abs_gap < best_time_gap:
                        best_time_gap = abs_gap
                        best_j = j

                if best_j >= 0:
                    # Merge: combine detections, deduplicate by frame
                    all_dets = chain.detections + candidates[best_j].detections
                    # Keep one detection per frame (prefer from the longer track)
                    by_frame: dict[int, SimpleDetection] = {}
                    for d in all_dets:
                        if d.frame_idx not in by_frame:
                            by_frame[d.frame_idx] = d
                    merged_dets = sorted(by_frame.values(), key=lambda d: d.frame_idx)
                    new_chain = SimpleTrack(
                        track_id=chain.track_id,
                        detections=merged_dets,
                        x=merged_dets[-1].x,
                        y=merged_dets[-1].y,
                    )
                    # Recompute velocity from last few detections
                    if len(merged_dets) >= 2:
                        d_prev = merged_dets[-2]
                        d_last = merged_dets[-1]
                        dt = d_last.frame_idx - d_prev.frame_idx
                        if dt > 0:
                            new_chain.vx = (d_last.x - d_prev.x) / dt
                            new_chain.vy = (d_last.y - d_prev.y) / dt
                    chain = new_chain
                    used[best_j] = True
                    changed = True

            stitched.append(chain)

        return stitched
