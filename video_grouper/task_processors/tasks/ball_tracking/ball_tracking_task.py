"""Ball tracking task for processing videos through the ML ball tracking pipeline."""

import logging
from pathlib import Path
from typing import Dict

from ..base_task import BaseTask
from ...queue_type import QueueType
from video_grouper.utils.config import BallTrackingConfig

logger = logging.getLogger(__name__)


class BallTrackingTask(BaseTask):
    """Task for processing a video through the ball tracking + virtual camera pipeline."""

    def __init__(
        self,
        group_dir: Path,
        input_path: str,
        output_path: str,
        ball_tracking_config: BallTrackingConfig,
    ):
        self.group_dir = group_dir
        self.input_path = input_path
        self.output_path = output_path
        self.ball_tracking_config = ball_tracking_config

    @classmethod
    def queue_type(cls) -> QueueType:
        return QueueType.TRACKING

    @property
    def task_type(self) -> str:
        return "ball_tracking_process"

    def get_item_path(self) -> str:
        return str(self.group_dir)

    def serialize(self) -> Dict[str, object]:
        return {
            "task_type": self.task_type,
            "group_dir": str(self.group_dir),
            "input_path": self.input_path,
            "output_path": self.output_path,
            "ball_tracking_config": {
                "model_path": self.ball_tracking_config.model_path,
                "confidence": self.ball_tracking_config.confidence,
                "output_width": self.ball_tracking_config.output_width,
                "output_height": self.ball_tracking_config.output_height,
                "camera_fov": self.ball_tracking_config.camera_fov,
                "camera_smoothing": self.ball_tracking_config.camera_smoothing,
                "device": self.ball_tracking_config.device,
                "enabled": self.ball_tracking_config.enabled,
            },
        }

    async def execute(self) -> bool:
        """Execute the ball tracking pipeline on the video.

        1. Run detection + tracking pipeline -> ball_track.json
        2. Render virtual camera output -> output.mp4
        """
        try:
            logger.info(f"BALL_TRACKING: Processing group {self.group_dir.name}")

            model_path = self.ball_tracking_config.model_path
            if not model_path or not Path(model_path).exists():
                logger.error(f"BALL_TRACKING: Model not found at {model_path}")
                return False

            from video_grouper.ball_tracking.pipeline import process_video
            from video_grouper.ball_tracking.virtual_camera import render_video

            track_path = self.group_dir / "ball_track.json"

            # Stage 1: Detection + Tracking
            logger.info(
                f"BALL_TRACKING: Running detection + tracking on {self.input_path}"
            )
            process_video(
                video_path=Path(self.input_path),
                model_path=Path(model_path),
                output_path=track_path,
                device=self.ball_tracking_config.device,
                confidence=self.ball_tracking_config.confidence,
            )

            # Stage 2: Virtual Camera Rendering
            logger.info(
                f"BALL_TRACKING: Rendering virtual camera to {self.output_path}"
            )
            render_video(
                video_path=Path(self.input_path),
                track_path=track_path,
                output_path=Path(self.output_path),
                output_w=self.ball_tracking_config.output_width,
                output_h=self.ball_tracking_config.output_height,
            )

            logger.info(
                f"BALL_TRACKING: Successfully processed group {self.group_dir.name}"
            )
            return True

        except Exception as e:
            logger.error(
                f"BALL_TRACKING: Error processing group {self.group_dir.name}: {e}"
            )
            return False

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "BallTrackingTask":
        config = BallTrackingConfig(
            model_path=data["ball_tracking_config"].get("model_path"),
            confidence=data["ball_tracking_config"].get("confidence", 0.25),
            output_width=data["ball_tracking_config"].get("output_width", 1920),
            output_height=data["ball_tracking_config"].get("output_height", 1080),
            camera_fov=data["ball_tracking_config"].get("camera_fov", 60.0),
            camera_smoothing=data["ball_tracking_config"].get("camera_smoothing", 0.85),
            device=data["ball_tracking_config"].get("device", "cpu"),
            enabled=data["ball_tracking_config"].get("enabled", True),
        )
        return cls(
            group_dir=Path(data["group_dir"]),
            input_path=data["input_path"],
            output_path=data["output_path"],
            ball_tracking_config=config,
        )

    @classmethod
    def deserialize(cls, data: Dict[str, object]) -> "BallTrackingTask":
        return cls.from_dict(data)

    def __str__(self):
        return f"BallTrackingTask(group_dir={self.group_dir})"

    def __eq__(self, other):
        if not isinstance(other, BallTrackingTask):
            return False
        return self.group_dir == other.group_dir and self.input_path == other.input_path

    def __hash__(self):
        return hash((self.group_dir, self.input_path))
