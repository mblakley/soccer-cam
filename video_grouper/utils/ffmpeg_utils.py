import os
import asyncio
import logging
import shutil
from typing import Optional

import av

logger = logging.getLogger(__name__)

# Default timeout for FFmpeg operations (30 minutes).
# Long videos (2+ hours of 180-degree footage) can take a while to process.
FFMPEG_TIMEOUT = 1800


def _scaled_timeout(file_paths: list[str], base_timeout: int = FFMPEG_TIMEOUT) -> int:
    """Scale the FFmpeg timeout based on total input file size.

    Uses 30 seconds per MB with a floor of *base_timeout* so large
    multi-hour recordings don't hit an artificial limit.
    """
    total_bytes = 0
    for p in file_paths:
        try:
            total_bytes += os.path.getsize(p)
        except OSError:
            pass
    size_based = int(total_bytes / (1024 * 1024) * 30)
    return max(base_timeout, size_based)


def _cleanup_temp(path: str) -> None:
    """Remove a temp file, ignoring errors if it doesn't exist."""
    try:
        os.remove(path)
    except OSError:
        pass


async def _run_in_thread_with_timeout(func, *args, timeout=FFMPEG_TIMEOUT):
    """Run a synchronous function in a thread pool with a timeout.

    PyAV operations are synchronous C code. This wrapper runs them off the
    event loop. On timeout the caller gets TimeoutError but the C thread
    finishes in background (acceptable because timeouts rarely fire).
    """
    return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)


def get_default_date_format():
    return "%Y-%m-%d %H:%M:%S"


async def verify_ffmpeg_install() -> bool:
    """Verify that PyAV (bundled FFmpeg) is available."""
    try:
        return av.codec.Codec("h264", "r") is not None
    except Exception as e:
        logger.error(f"Error verifying FFmpeg/PyAV installation: {e}")
        return False


def _get_video_duration_sync(file_path: str) -> Optional[float]:
    """Synchronous implementation: get video duration via av.open metadata."""
    try:
        with av.open(file_path) as container:
            if container.duration is not None:
                return container.duration / av.time_base
            # Fallback: check the first video stream's duration
            for stream in container.streams.video:
                if stream.duration is not None and stream.time_base is not None:
                    return float(stream.duration * stream.time_base)
    except Exception:
        raise
    return None


async def get_video_duration(file_path: str) -> Optional[float]:
    """Get the duration of a video file using PyAV."""
    try:
        return await _run_in_thread_with_timeout(
            _get_video_duration_sync, file_path, timeout=60
        )
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return None


async def verify_mp4_duration(
    dav_file: str, mp4_file: str, tolerance: float = 0.1
) -> bool:
    """Verify that the MP4 file duration matches the DAV file duration."""
    try:
        if not os.path.exists(dav_file) or not os.path.exists(mp4_file):
            return False

        dav_duration = await get_video_duration(dav_file)
        mp4_duration = await get_video_duration(mp4_file)

        if dav_duration is None or mp4_duration is None:
            return False

        duration_diff = abs(dav_duration - mp4_duration)
        return duration_diff <= (dav_duration * tolerance)

    except Exception as e:
        logger.error(f"Error verifying MP4 duration: {e}")
        return False


def _remux_dav_to_mp4_sync(file_path: str, output_path: str) -> str:
    """Synchronous implementation: remux DAV -> MP4 (video copy, AAC re-encode)."""
    with av.open(file_path) as input_container:
        with av.open(
            output_path, "w", options={"movflags": "faststart"}
        ) as output_container:
            # Set up streams
            in_video_stream = None
            in_audio_stream = None
            out_video_stream = None
            out_audio_stream = None

            for stream in input_container.streams:
                if stream.type == "video" and in_video_stream is None:
                    in_video_stream = stream
                    out_video_stream = output_container.add_stream_from_template(
                        in_video_stream
                    )
                elif stream.type == "audio" and in_audio_stream is None:
                    in_audio_stream = stream
                    out_audio_stream = output_container.add_stream(
                        "aac", rate=in_audio_stream.rate or 44100
                    )
                    out_audio_stream.bit_rate = 192000

            if in_video_stream is None:
                raise ValueError(f"No video stream found in {file_path}")

            # Track first DTS per stream to normalize timestamps
            first_dts = {}

            # Demux and remux
            streams_to_demux = []
            if in_video_stream:
                streams_to_demux.append(in_video_stream)
            if in_audio_stream:
                streams_to_demux.append(in_audio_stream)

            for packet in input_container.demux(streams_to_demux):
                if packet.dts is None:
                    continue

                try:
                    if packet.stream == in_video_stream:
                        # Normalize DTS
                        if in_video_stream.index not in first_dts:
                            first_dts[in_video_stream.index] = packet.dts
                        packet.dts -= first_dts[in_video_stream.index]
                        packet.pts -= first_dts[in_video_stream.index]
                        if packet.dts < 0:
                            continue
                        packet.stream = out_video_stream
                        output_container.mux(packet)

                    elif packet.stream == in_audio_stream and out_audio_stream:
                        # Decode and re-encode audio to AAC
                        for frame in packet.decode():
                            frame.pts = None  # Let encoder set pts
                            for out_packet in out_audio_stream.encode(frame):
                                output_container.mux(out_packet)
                except (av.InvalidDataError, av.error.FFmpegError):
                    continue

            # Flush audio encoder
            if out_audio_stream:
                for out_packet in out_audio_stream.encode(None):
                    output_container.mux(out_packet)

    return output_path


async def async_convert_file(file_path: str) -> Optional[str]:
    """Converts a video file to MP4 format using PyAV.

    Video stream is copied (no re-encoding), audio is re-encoded to AAC 192k.
    """
    if not os.path.exists(file_path):
        logger.error(f"Input file not found: {file_path}")
        return None

    base, ext = os.path.splitext(file_path)
    output_path = base + ".mp4" if ext.lower() == ".dav" else file_path + ".mp4"

    logger.info(
        f"Converting {os.path.basename(file_path)} to {os.path.basename(output_path)}"
    )

    try:
        await _run_in_thread_with_timeout(
            _remux_dav_to_mp4_sync, file_path, output_path
        )
        logger.info(
            f"Successfully converted {os.path.basename(file_path)} to {os.path.basename(output_path)}"
        )
        return output_path
    except asyncio.TimeoutError:
        logger.error(
            f"Conversion timed out for {os.path.basename(file_path)} after {FFMPEG_TIMEOUT}s"
        )
        return None
    except Exception as e:
        logger.error(f"Failed to convert {os.path.basename(file_path)}: {e}")
        return None


def _is_frame_corrupt(image) -> bool:
    """Check if a decoded frame is corrupt (solid green, half-green, etc.)."""
    w, h = image.size

    # Check full frame and sub-regions (HEVC corruption often appears as a
    # solid-green right half while the left decodes partially).
    regions = [
        (0, 0, w, h),  # full frame
        (w // 2, 0, w, h),  # right half
        (0, h // 2, w, h),  # bottom half
    ]
    for box in regions:
        crop = image.crop(box)
        extrema = crop.getextrema()
        # If all RGB channels span < 10 values, the region is near-solid = corrupt
        if all((hi - lo) < 10 for lo, hi in extrema[:3]):
            return True
    return False


def _create_screenshot_sync(
    video_path: str, output_path: str, time_offset: str
) -> bool:
    """Synchronous implementation: extract a single frame as JPEG.

    For HEVC stream-copy files, seeking can land between keyframes and produce
    corrupt (green) frames.  Strategy: try the requested offset first, then
    fall back to progressively later positions until a clean frame is found.
    """
    # Parse time offset string (HH:MM:SS or seconds)
    try:
        parts = time_offset.split(":")
        if len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            seconds = int(parts[0]) * 60 + float(parts[1])
        else:
            seconds = float(time_offset)
    except (ValueError, IndexError):
        seconds = 1.0

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        duration = float(stream.duration * stream.time_base) if stream.duration else 0

        # Try several seek positions: requested offset, then 30s, 60s, 10% in, 25% in
        seek_targets = [seconds]
        for extra in [30, 60]:
            if extra not in seek_targets:
                seek_targets.append(extra)
        if duration > 0:
            for pct in [0.10, 0.25]:
                t = duration * pct
                if t not in seek_targets:
                    seek_targets.append(t)

        for seek_sec in seek_targets:
            if duration > 0 and seek_sec >= duration:
                continue

            target_pts = int(seek_sec / stream.time_base)
            container.seek(target_pts, stream=stream)

            # Decode up to 60 frames (~3s at 20fps) to find a clean one
            for i, frame in enumerate(container.decode(video=0)):
                if i >= 60:
                    break
                image = frame.to_image()
                if not _is_frame_corrupt(image):
                    image.save(output_path, "JPEG", quality=95)
                    return True

        # All attempts failed
        logger.warning(
            f"Could not find a non-corrupt frame in {os.path.basename(video_path)}"
        )
        return False


async def create_screenshot(
    video_path: str, output_path: str, time_offset: str = "00:00:01"
) -> bool:
    """Creates a screenshot from a video file."""
    try:
        result = await _run_in_thread_with_timeout(
            _create_screenshot_sync, video_path, output_path, time_offset, timeout=60
        )
        if result:
            logger.info(
                f"Successfully created screenshot for {os.path.basename(video_path)}"
            )
        return result
    except Exception as e:
        logger.error(
            f"Failed to create screenshot for {os.path.basename(video_path)}: {e}"
        )
        return False


def _trim_video_sync(
    input_path: str, output_path: str, start_offset: str, duration: Optional[str]
) -> bool:
    """Synchronous implementation: trim video with stream copy."""
    # Parse start offset
    try:
        parts = start_offset.split(":")
        if len(parts) == 3:
            start_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            start_seconds = int(parts[0]) * 60 + float(parts[1])
        else:
            start_seconds = float(start_offset)
    except (ValueError, IndexError):
        start_seconds = 0.0

    # Parse duration
    duration_seconds = None
    if duration:
        try:
            parts = duration.split(":")
            if len(parts) == 3:
                duration_seconds = (
                    int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                )
            elif len(parts) == 2:
                duration_seconds = int(parts[0]) * 60 + float(parts[1])
            else:
                duration_seconds = float(duration)
        except (ValueError, IndexError):
            pass

    with av.open(input_path) as input_container:
        with av.open(
            output_path, "w", options={"movflags": "faststart"}
        ) as output_container:
            # Create output streams as copies of input streams
            stream_map = {}
            for in_stream in input_container.streams:
                if in_stream.type in ("video", "audio"):
                    out_stream = output_container.add_stream_from_template(in_stream)
                    stream_map[in_stream] = out_stream

            if not stream_map:
                raise ValueError(f"No video/audio streams found in {input_path}")

            # Seek to start position
            video_stream = input_container.streams.video[0]
            target_pts = int(start_seconds / video_stream.time_base)
            input_container.seek(target_pts, stream=video_stream)

            # Track first DTS per stream for normalization
            first_dts = {}
            end_pts = None

            if duration_seconds is not None:
                end_pts = int(
                    (start_seconds + duration_seconds) / video_stream.time_base
                )

            for packet in input_container.demux(list(stream_map.keys())):
                if packet.dts is None:
                    continue

                # Check if we've passed the end time
                if (
                    end_pts is not None
                    and packet.stream == video_stream
                    and packet.dts > end_pts
                ):
                    break

                try:
                    in_stream = packet.stream
                    out_stream = stream_map[in_stream]

                    # Normalize timestamps
                    if in_stream.index not in first_dts:
                        first_dts[in_stream.index] = packet.dts
                    packet.dts -= first_dts[in_stream.index]
                    packet.pts -= first_dts[in_stream.index]
                    if packet.dts < 0:
                        continue

                    packet.stream = out_stream
                    output_container.mux(packet)
                except (av.InvalidDataError, av.error.FFmpegError):
                    continue

    return True


async def trim_video(
    input_path: str, output_path: str, start_offset: str, duration: Optional[str] = None
) -> bool:
    """Trims a video file using stream copy via PyAV.

    Writes to a temp file first, then renames on success so a crash
    never leaves a partial MP4 at the final path.
    """
    logger.info(
        f"Trimming {os.path.basename(input_path)} to {os.path.basename(output_path)}"
    )
    temp_output = output_path + ".tmp"
    try:
        result = await _run_in_thread_with_timeout(
            _trim_video_sync, input_path, temp_output, start_offset, duration
        )
        if result:
            os.replace(temp_output, output_path)
            logger.info(
                f"Successfully trimmed {os.path.basename(input_path)} to {os.path.basename(output_path)}"
            )
        else:
            _cleanup_temp(temp_output)
        return result
    except asyncio.TimeoutError:
        logger.error(f"Trim timed out after {FFMPEG_TIMEOUT}s")
        _cleanup_temp(temp_output)
        return False
    except Exception as e:
        logger.error(f"Error during trim: {e}")
        _cleanup_temp(temp_output)
        return False


def _combine_videos_sync(
    file_paths: list[str],
    output_path: str,
    camera_name: str | None = None,
    camera_type: str | None = None,
) -> bool:
    """Synchronous implementation: concatenate multiple video files (video copy, AAC re-encode).

    Uses stream copy for video regardless of codec (H.264, HEVC, etc.) for speed.
    HEVC-to-H.264 transcoding is deferred to later pipeline stages (trim/autocam)
    where the video is shorter and transcoding is practical.
    """
    if not file_paths:
        raise ValueError("No files to combine")

    with av.open(
        output_path, "w", format="mp4", options={"movflags": "faststart"}
    ) as output_container:
        # Write camera metadata if available
        if camera_name:
            output_container.metadata["camera_name"] = camera_name
            if camera_type:
                output_container.metadata["camera_type"] = camera_type
                output_container.metadata["comment"] = (
                    f"Camera: {camera_name} ({camera_type})"
                )
            else:
                output_container.metadata["comment"] = f"Camera: {camera_name}"
        out_video_stream = None
        out_audio_stream = None

        # Probe the first file to set up output streams
        with av.open(file_paths[0]) as probe:
            for stream in probe.streams:
                if stream.type == "video" and out_video_stream is None:
                    out_video_stream = output_container.add_stream_from_template(stream)
                elif stream.type == "audio" and out_audio_stream is None:
                    out_audio_stream = output_container.add_stream(
                        "aac", rate=stream.rate or 44100
                    )
                    out_audio_stream.bit_rate = 192000

        if out_video_stream is None:
            raise ValueError("No video stream found in first input file")

        return _combine_copy(
            file_paths, output_container, out_video_stream, out_audio_stream
        )


def _combine_copy(
    file_paths: list[str], output_container, out_video_stream, out_audio_stream
) -> bool:
    """Concatenate files using stream copy (fast path for H.264 input)."""
    video_pts_offset = 0

    for file_path in file_paths:
        with av.open(file_path) as input_container:
            in_video = None
            in_audio = None
            for stream in input_container.streams:
                if stream.type == "video" and in_video is None:
                    in_video = stream
                elif stream.type == "audio" and in_audio is None:
                    in_audio = stream

            streams_to_demux = []
            if in_video:
                streams_to_demux.append(in_video)
            if in_audio:
                streams_to_demux.append(in_audio)

            first_dts = {}
            max_video_pts = 0

            for packet in input_container.demux(streams_to_demux):
                if packet.dts is None:
                    continue

                try:
                    if packet.stream == in_video:
                        # Normalize and offset
                        if in_video.index not in first_dts:
                            first_dts[in_video.index] = packet.dts
                        packet.dts -= first_dts[in_video.index]
                        packet.pts -= first_dts[in_video.index]
                        if packet.dts < 0:
                            continue

                        # Track max pts for offset calculation
                        if packet.pts is not None:
                            end_pts = packet.pts + (packet.duration or 0)
                            if end_pts > max_video_pts:
                                max_video_pts = end_pts

                        packet.dts += video_pts_offset
                        packet.pts += video_pts_offset
                        packet.stream = out_video_stream
                        output_container.mux(packet)

                    elif packet.stream == in_audio and out_audio_stream:
                        for frame in packet.decode():
                            frame.pts = None
                            for out_packet in out_audio_stream.encode(frame):
                                output_container.mux(out_packet)
                except (av.InvalidDataError, av.error.FFmpegError):
                    continue

            video_pts_offset += max_video_pts

    # Flush audio encoder
    if out_audio_stream:
        for out_packet in out_audio_stream.encode(None):
            output_container.mux(out_packet)

    return True


async def combine_videos(
    file_paths: list[str],
    output_path: str,
    camera_name: str | None = None,
    camera_type: str | None = None,
) -> bool:
    """Combines multiple video files into a single MP4 using PyAV.

    Writes to a temp file first, then renames on success so a crash
    never leaves a partial MP4 at the final path.

    Args:
        file_paths: List of input file paths to concatenate.
        output_path: Path for the combined output file.
        camera_name: Optional camera name to embed in MP4 metadata.
        camera_type: Optional camera type to embed in MP4 metadata.
    """
    logger.info(
        f"Combining {len(file_paths)} videos to {os.path.basename(output_path)}"
    )
    temp_output = output_path + ".tmp"
    timeout = _scaled_timeout(file_paths)
    try:
        result = await _run_in_thread_with_timeout(
            _combine_videos_sync,
            file_paths,
            temp_output,
            camera_name,
            camera_type,
            timeout=timeout,
        )
        if result:
            os.replace(temp_output, output_path)
            logger.info(
                f"Successfully combined videos to {os.path.basename(output_path)}"
            )
        else:
            _cleanup_temp(temp_output)
        return result
    except asyncio.TimeoutError:
        logger.error(f"Combine timed out after {timeout}s")
        _cleanup_temp(temp_output)
        return False
    except Exception as e:
        logger.error(f"Error during combine: {e}")
        _cleanup_temp(temp_output)
        return False


def _extract_clip_copy_sync(
    input_path: str, start_sec: float, end_sec: float, output_path: str
) -> bool:
    """Synchronous implementation: extract clip with stream copy."""
    with av.open(input_path) as input_container:
        with av.open(output_path, "w", format="mp4") as output_container:
            stream_map = {}
            for in_stream in input_container.streams:
                if in_stream.type in ("video", "audio"):
                    out_stream = output_container.add_stream_from_template(in_stream)
                    stream_map[in_stream] = out_stream

            video_stream = input_container.streams.video[0]
            target_pts = int(start_sec / video_stream.time_base)
            end_pts = int(end_sec / video_stream.time_base)
            input_container.seek(target_pts, stream=video_stream)

            first_dts = {}

            for packet in input_container.demux(list(stream_map.keys())):
                if packet.dts is None:
                    continue

                if packet.stream == video_stream and packet.dts > end_pts:
                    break

                try:
                    in_stream = packet.stream
                    out_stream = stream_map[in_stream]

                    if in_stream.index not in first_dts:
                        first_dts[in_stream.index] = packet.dts
                    packet.dts -= first_dts[in_stream.index]
                    packet.pts -= first_dts[in_stream.index]
                    if packet.dts < 0:
                        continue

                    packet.stream = out_stream
                    output_container.mux(packet)
                except (av.InvalidDataError, av.error.FFmpegError):
                    continue

    return True


def _extract_clip_reencode_sync(
    input_path: str, start_sec: float, end_sec: float, output_path: str
) -> bool:
    """Synchronous implementation: extract clip with full re-encode (fallback)."""
    with av.open(input_path) as input_container:
        with av.open(output_path, "w", format="mp4") as output_container:
            in_video = input_container.streams.video[0]
            in_audio = None
            for stream in input_container.streams:
                if stream.type == "audio":
                    in_audio = stream
                    break

            # Set up output video with libx264
            out_video = output_container.add_stream(
                "libx264", rate=in_video.average_rate
            )
            out_video.width = in_video.width
            out_video.height = in_video.height
            out_video.pix_fmt = "yuv420p"
            out_video.options = {"preset": "fast", "crf": "18"}

            out_audio = None
            if in_audio:
                out_audio = output_container.add_stream(
                    "aac", rate=in_audio.rate or 44100
                )
                out_audio.bit_rate = 128000

            # Seek to start
            target_pts = int(start_sec / in_video.time_base)
            end_pts = int(end_sec / in_video.time_base)
            input_container.seek(target_pts, stream=in_video)

            streams_to_decode = [in_video]
            if in_audio:
                streams_to_decode.append(in_audio)

            for packet in input_container.demux(streams_to_decode):
                if packet.stream == in_video:
                    for frame in packet.decode():
                        if frame.pts is not None and frame.pts > end_pts:
                            break
                        frame.pts = None
                        for out_packet in out_video.encode(frame):
                            output_container.mux(out_packet)
                    else:
                        continue
                    break
                elif packet.stream == in_audio and out_audio:
                    for frame in packet.decode():
                        frame.pts = None
                        for out_packet in out_audio.encode(frame):
                            output_container.mux(out_packet)

            # Flush encoders
            for out_packet in out_video.encode(None):
                output_container.mux(out_packet)
            if out_audio:
                for out_packet in out_audio.encode(None):
                    output_container.mux(out_packet)

    return True


async def extract_clip(
    input_path: str,
    start_sec: float,
    end_sec: float,
    output_path: str,
    timeout: int = FFMPEG_TIMEOUT,
) -> str:
    """Extract a clip from a video file.

    Uses stream copy for fast extraction. Falls back to re-encoding if
    stream copy fails (e.g., keyframe alignment issues).

    Returns:
        The output_path on success.

    Raises:
        RuntimeError: If extraction fails.
    """
    duration = end_sec - start_sec

    try:
        await _run_in_thread_with_timeout(
            _extract_clip_copy_sync,
            input_path,
            start_sec,
            end_sec,
            output_path,
            timeout=timeout,
        )
    except Exception as copy_err:
        logger.warning(
            f"Stream copy failed for clip, falling back to re-encode: {copy_err}"
        )
        try:
            await _run_in_thread_with_timeout(
                _extract_clip_reencode_sync,
                input_path,
                start_sec,
                end_sec,
                output_path,
                timeout=timeout,
            )
        except Exception as enc_err:
            raise RuntimeError(f"Clip extraction failed: {enc_err}") from enc_err

    logger.info(f"Extracted clip: {output_path} ({duration:.1f}s)")
    return output_path


def _compile_clips_sync(clip_paths: list[str], output_path: str) -> bool:
    """Synchronous implementation: concatenate clips with full re-encode."""
    with av.open(output_path, "w", format="mp4") as output_container:
        out_video = None
        out_audio = None

        # Probe first clip for stream parameters
        with av.open(clip_paths[0]) as probe:
            in_video = probe.streams.video[0]
            in_audio = None
            for stream in probe.streams:
                if stream.type == "audio":
                    in_audio = stream
                    break

            out_video = output_container.add_stream(
                "libx264", rate=in_video.average_rate
            )
            out_video.width = in_video.width
            out_video.height = in_video.height
            out_video.pix_fmt = "yuv420p"
            out_video.options = {"preset": "fast", "crf": "18"}

            if in_audio:
                out_audio = output_container.add_stream(
                    "aac", rate=in_audio.rate or 44100
                )
                out_audio.bit_rate = 128000

        for clip_path in clip_paths:
            with av.open(clip_path) as input_container:
                streams_to_decode = list(input_container.streams.video)
                if out_audio:
                    streams_to_decode.extend(list(input_container.streams.audio))

                for packet in input_container.demux(streams_to_decode):
                    try:
                        if packet.stream.type == "video":
                            for frame in packet.decode():
                                frame.pts = None
                                for out_packet in out_video.encode(frame):
                                    output_container.mux(out_packet)
                        elif packet.stream.type == "audio" and out_audio:
                            for frame in packet.decode():
                                frame.pts = None
                                for out_packet in out_audio.encode(frame):
                                    output_container.mux(out_packet)
                    except (av.InvalidDataError, av.error.FFmpegError):
                        continue

        # Flush encoders
        for out_packet in out_video.encode(None):
            output_container.mux(out_packet)
        if out_audio:
            for out_packet in out_audio.encode(None):
                output_container.mux(out_packet)

    return True


async def compile_clips(
    clip_paths: list[str],
    output_path: str,
    timeout: int = FFMPEG_TIMEOUT,
) -> str:
    """Compile multiple clips into a single video file.

    Re-encodes to ensure consistent format across clips.

    Returns:
        The output_path on success.

    Raises:
        RuntimeError: If compilation fails.
        ValueError: If clip_paths is empty.
    """
    if not clip_paths:
        raise ValueError("No clips to compile")

    if len(clip_paths) == 1:
        shutil.copy2(clip_paths[0], output_path)
        logger.info(f"Single clip, copied to: {output_path}")
        return output_path

    try:
        await _run_in_thread_with_timeout(
            _compile_clips_sync, clip_paths, output_path, timeout=timeout
        )
    except Exception as e:
        raise RuntimeError(f"Clip compilation failed: {e}") from e

    logger.info(f"Compiled {len(clip_paths)} clips into: {output_path}")
    return output_path
