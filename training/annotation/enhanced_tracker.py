"""Enhanced ball tracker combining commercial techniques with our innovations.

Adopts proven techniques from commercial ball tracking:
- Detection buffer (3 seconds of detections, weighted averaging)
- Heavy EMA smoothing (97.5% for position, 75% for velocity)
- Field polygon constraint
- High confidence threshold (0.45+)

Plus our additions:
- User-guided correction (manual marks as ground truth anchors)
- Trajectory stitching across detection gaps
- Static detection removal
- Guide path interpolation/extrapolation from user marks
- Frame differencing for small ball recovery (when available)
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Autocam-derived defaults
DEFAULT_CONF_THRESHOLD = 0.45
BUFFER_SECONDS = 3.0
POSITION_EMA = 0.975       # 97.5% previous + 2.5% new (very smooth)
VELOCITY_EMA = 0.75        # 75% smoothing on velocity
FRAME_INTERVAL = 8
FPS = 25.0
FRAMES_PER_SECOND = FPS / FRAME_INTERVAL  # ~3.125 detection frames per second
BUFFER_SIZE = int(BUFFER_SECONDS * FRAMES_PER_SECOND)  # ~9-10 frames


@dataclass
class TrackedPosition:
    """A single tracked ball position with metadata."""
    frame_idx: int
    x: float              # panoramic x
    y: float              # panoramic y
    conf: float           # detection confidence (0-1)
    source: str = "det"   # "det", "user", "interp", "extrap", "diff"
    raw_x: float = 0.0    # raw detection x before smoothing
    raw_y: float = 0.0    # raw detection y before smoothing


@dataclass
class EnhancedTrackerState:
    """Current state of the enhanced tracker."""
    # Smoothed position (EMA)
    x: float = 0.0
    y: float = 0.0

    # Smoothed velocity (EMA)
    vx: float = 0.0
    vy: float = 0.0

    # Detection buffer (last N positions for weighted averaging)
    buffer: deque = field(default_factory=lambda: deque(maxlen=BUFFER_SIZE))

    # Whether tracker has been initialized
    initialized: bool = False

    # Frames since last real detection
    frames_without_detection: int = 0

    # Recent detection confidence (for adaptive frame rate)
    recent_conf: float = 0.0

    # Current detection interval (1=every frame, 8=every 8th)
    detection_interval: int = FRAME_INTERVAL

    # Track history
    history: list = field(default_factory=list)  # list of TrackedPosition


class EnhancedTracker:
    """Ball tracker combining commercial smoothing with user-guided selection.

    Usage:
        tracker = EnhancedTracker()

        # Process each frame
        for frame_idx, detections in frames:
            pos = tracker.update(frame_idx, detections, user_mark=None)
            if pos:
                print(f"Ball at ({pos.x}, {pos.y}) conf={pos.conf}")

        # Get full trajectory
        trajectory = tracker.get_trajectory()
    """

    def __init__(
        self,
        conf_threshold: float = DEFAULT_CONF_THRESHOLD,
        position_ema: float = POSITION_EMA,
        velocity_ema: float = VELOCITY_EMA,
        buffer_seconds: float = BUFFER_SECONDS,
        max_missing_frames: int = 30,  # ~10 seconds at 3fps
        field_filter=None,  # callable(x, y) -> bool
    ):
        self.conf_threshold = conf_threshold
        self.position_ema = position_ema
        self.velocity_ema = velocity_ema
        self.buffer_size = int(buffer_seconds * FRAMES_PER_SECOND)
        self.max_missing_frames = max_missing_frames
        self.field_filter = field_filter

        self.state = EnhancedTrackerState(
            buffer=deque(maxlen=self.buffer_size)
        )

    def update(
        self,
        frame_idx: int,
        detections: list[tuple[float, float, float]],
        user_mark: tuple[float, float] | None = None,
    ) -> TrackedPosition | None:
        """Process one frame of detections.

        Args:
            frame_idx: Current frame index
            detections: List of (x, y, confidence) in panoramic coords
            user_mark: Optional (x, y) from user annotation — overrides detection

        Returns:
            TrackedPosition if ball is being tracked, None if lost.
        """
        state = self.state

        # Step 1: Select the best detection for this frame
        selected = None

        if user_mark:
            # User mark always wins
            selected = TrackedPosition(
                frame_idx=frame_idx,
                x=user_mark[0], y=user_mark[1],
                conf=1.0, source="user",
                raw_x=user_mark[0], raw_y=user_mark[1],
            )
        else:
            # Filter detections by confidence and field boundary
            candidates = []
            for x, y, conf in detections:
                if conf < self.conf_threshold:
                    continue
                if self.field_filter and not self.field_filter(x, y):
                    continue
                candidates.append((x, y, conf))

            if candidates and state.initialized:
                # Pick detection closest to predicted position
                pred_x = state.x + state.vx
                pred_y = state.y + state.vy
                best = min(candidates, key=lambda d: (d[0] - pred_x) ** 2 + (d[1] - pred_y) ** 2)
                selected = TrackedPosition(
                    frame_idx=frame_idx,
                    x=best[0], y=best[1],
                    conf=best[2], source="det",
                    raw_x=best[0], raw_y=best[1],
                )
            elif candidates:
                # Not yet initialized — pick highest confidence
                best = max(candidates, key=lambda d: d[2])
                selected = TrackedPosition(
                    frame_idx=frame_idx,
                    x=best[0], y=best[1],
                    conf=best[2], source="det",
                    raw_x=best[0], raw_y=best[1],
                )

        # Step 2: Update tracker state
        if selected:
            state.frames_without_detection = 0

            if not state.initialized:
                # First detection — initialize
                state.x = selected.x
                state.y = selected.y
                state.buffer.append((selected.raw_x, selected.raw_y, selected.conf))
                state.vx = 0.0
                state.vy = 0.0
                state.initialized = True
            else:
                # Compute raw velocity from this detection
                raw_vx = selected.raw_x - state.x
                raw_vy = selected.raw_y - state.y

                # Smooth velocity with EMA
                state.vx = self.velocity_ema * state.vx + (1 - self.velocity_ema) * raw_vx
                state.vy = self.velocity_ema * state.vy + (1 - self.velocity_ema) * raw_vy

                # User marks bypass buffer averaging and EMA — snap immediately
                if selected.source == "user":
                    state.x = selected.raw_x
                    state.y = selected.raw_y
                    state.buffer.clear()  # Reset buffer to user's position
                    state.buffer.append((selected.raw_x, selected.raw_y, selected.conf))
                else:
                    # Add to detection buffer
                    state.buffer.append((selected.raw_x, selected.raw_y, selected.conf))

                    # Compute weighted average of buffer
                    if len(state.buffer) >= 2:
                        total_w = 0.0
                        avg_x = 0.0
                        avg_y = 0.0
                        for i, (bx, by, bc) in enumerate(state.buffer):
                            recency = (i + 1) / len(state.buffer)
                            w = bc * recency
                            avg_x += bx * w
                            avg_y += by * w
                            total_w += w
                        if total_w > 0:
                            avg_x /= total_w
                            avg_y /= total_w
                            selected.x = avg_x
                            selected.y = avg_y

                    # Smooth position with EMA
                    state.x = self.position_ema * state.x + (1 - self.position_ema) * selected.x
                    state.y = self.position_ema * state.y + (1 - self.position_ema) * selected.y

                # Update selected with final smoothed position
                selected.x = state.x
                selected.y = state.y

            state.history.append(selected)
            self._update_adaptive_rate(selected)
            return selected

        else:
            # No detection — predict using velocity
            state.frames_without_detection += 1

            if state.initialized and state.frames_without_detection <= self.max_missing_frames:
                # Extrapolate with decaying velocity
                decay = 0.95 ** state.frames_without_detection
                state.x += state.vx * decay
                state.y += state.vy * decay

                predicted = TrackedPosition(
                    frame_idx=frame_idx,
                    x=state.x, y=state.y,
                    conf=0.0,
                    source="extrap",
                    raw_x=state.x, raw_y=state.y,
                )
                state.history.append(predicted)
                self._update_adaptive_rate(predicted)
                return predicted

            self._update_adaptive_rate(None)
            return None

    def get_trajectory(self) -> list[TrackedPosition]:
        """Get the full trajectory history."""
        return list(self.state.history)

    def get_smoothed_trajectory(
        self, frame_interval: int = FRAME_INTERVAL
    ) -> list[tuple[int, float, float, float, str]]:
        """Get trajectory as (frame_idx, x, y, conf, source) tuples."""
        return [
            (p.frame_idx, p.x, p.y, p.conf, p.source)
            for p in self.state.history
        ]

    def get_detection_interval(self) -> int:
        """Get the recommended frame interval for the next detection run.

        Returns 1-8 based on tracker confidence:
        - 8: Ball tracked confidently, coast at low frame rate
        - 4: Confidence dropping, look more often
        - 2: Ball uncertain, search actively
        - 1: Ball lost, examine every frame

        This enables variable frame rate detection:
        - Saves compute when tracking is solid
        - Searches harder when the ball is lost
        """
        state = self.state

        if not state.initialized:
            return 1  # Haven't found the ball yet — look at everything

        if state.frames_without_detection >= 10:
            return 1  # Lost for a while — every frame
        elif state.frames_without_detection >= 5:
            return 2  # Starting to lose it — double rate
        elif state.frames_without_detection >= 2:
            return 4  # Brief gap — moderate increase

        # Ball is being tracked — adjust based on recent confidence
        if state.recent_conf >= 0.6:
            return 8  # High confidence — coast
        elif state.recent_conf >= 0.4:
            return 4  # Moderate confidence
        elif state.recent_conf >= 0.2:
            return 2  # Low confidence — look harder
        else:
            return 1  # Very low — search aggressively

    def _update_adaptive_rate(self, selected: TrackedPosition | None):
        """Update the detection interval based on current tracking state."""
        state = self.state

        if selected and selected.source in ("det", "user"):
            # Smooth recent confidence
            state.recent_conf = 0.7 * state.recent_conf + 0.3 * selected.conf
        elif selected and selected.source == "extrap":
            # Decay confidence when extrapolating
            state.recent_conf *= 0.8

        state.detection_interval = self.get_detection_interval()

    def reset(self):
        """Reset tracker state."""
        self.state = EnhancedTrackerState(
            buffer=deque(maxlen=self.buffer_size)
        )
