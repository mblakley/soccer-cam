"""Virtual PTZ camera rendering -- cylindrical to rectilinear dewarping.

Reads a ball_track.json file and the camera controller decisions to
render a 1920x1080 output video with the virtual camera following the ball.

Dewarping math is ported from the video-stitcher half-cylinder geometry:
../video-stitcher/frontend/src/features/panorama/PanoramaViewer.jsx
"""

import json
import logging
from pathlib import Path

import cv2
import numpy as np

from video_grouper.ball_tracking.camera_controller import CameraController, CameraState
from video_grouper.ball_tracking.coordinates import CameraProfile

logger = logging.getLogger(__name__)


def build_remap_tables(
    camera_state: CameraState,
    profile: CameraProfile,
    output_w: int = 1920,
    output_h: int = 1080,
) -> tuple[np.ndarray, np.ndarray]:
    """Build OpenCV remap tables for cylindrical-to-rectilinear dewarping.

    For each pixel in the output image, compute where it samples from
    in the cylindrical panoramic source.

    Args:
        camera_state: Virtual camera position and FOV
        profile: Source camera profile
        output_w: Output video width
        output_h: Output video height

    Returns:
        (map_x, map_y) arrays for cv2.remap()
    """
    fov_h = camera_state.fov
    fov_v = fov_h * (output_h / output_w)

    # Build direction vectors for each output pixel
    u = np.linspace(-0.5, 0.5, output_w, dtype=np.float32)
    v = np.linspace(-0.5, 0.5, output_h, dtype=np.float32)
    uu, vv = np.meshgrid(u, v)

    # Rectilinear: pixel direction = tan(angle)
    yaw_offsets = np.tan(uu * fov_h)
    pitch_offsets = np.tan(vv * fov_v)

    # Convert back to angular: these are the source yaw/pitch for each output pixel
    source_yaw = camera_state.yaw + np.arctan(yaw_offsets)
    source_pitch = camera_state.pitch + np.arctan(pitch_offsets)

    # Cylindrical projection: yaw/pitch -> pixel coordinates
    map_x = (source_yaw + profile.fov_h / 2) / profile.fov_h * profile.width
    map_y = (source_pitch + profile.fov_v / 2) / profile.fov_v * profile.height

    return map_x.astype(np.float32), map_y.astype(np.float32)


def render_video(
    video_path: Path,
    track_path: Path,
    output_path: Path,
    profile: CameraProfile | None = None,
    output_w: int = 1920,
    output_h: int = 1080,
) -> Path:
    """Render a virtual PTZ video from a panoramic source and ball track.

    Args:
        video_path: Path to the panoramic source video
        track_path: Path to ball_track.json
        output_path: Path for the output video
        profile: Camera profile (default: Dahua panoramic)
        output_w: Output video width
        output_h: Output video height

    Returns:
        Path to the rendered output video
    """
    if profile is None:
        profile = CameraProfile.dahua_panoramic()

    with open(track_path) as f:
        track_data = json.load(f)

    fps = track_data["fps"]
    frames = track_data["frames"]

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter.fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (output_w, output_h))

    controller = CameraController()
    cached_maps = None
    cached_state_key = None

    logger.info(
        "Rendering %d frames to %s (%dx%d)",
        len(frames),
        output_path,
        output_w,
        output_h,
    )

    for i, frame_data in enumerate(frames):
        ret, frame = cap.read()
        if not ret:
            break

        from video_grouper.ball_tracking.coordinates import AngularPosition

        target = AngularPosition(yaw=frame_data["yaw"], pitch=frame_data["pitch"])
        camera_state = controller.update(
            target,
            frame_data["confidence"],
            frame_data.get("vyaw", 0.0),
            frame_data.get("vpitch", 0.0),
        )

        # Cache remap tables when camera position hasn't moved much
        state_key = (
            round(camera_state.yaw, 3),
            round(camera_state.pitch, 3),
            round(camera_state.fov, 3),
        )
        if cached_maps is None or state_key != cached_state_key:
            cached_maps = build_remap_tables(camera_state, profile, output_w, output_h)
            cached_state_key = state_key

        output_frame = cv2.remap(
            frame, cached_maps[0], cached_maps[1], cv2.INTER_LINEAR
        )
        writer.write(output_frame)

        if i % 300 == 0:
            logger.info("Rendered frame %d/%d", i, len(frames))

    writer.release()
    cap.release()

    logger.info("Rendered %s", output_path)
    return output_path
