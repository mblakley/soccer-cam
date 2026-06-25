import asyncio
import logging
import os
import shutil


# ``av`` is loaded lazily via the proxy below. The tray PyInstaller
# bundle excludes PyAV (TRAY_EXCLUDES in VideoGrouper.spec) to dodge an
# onnxruntime/PyQt6 MSVCP140.dll initialization conflict; any module
# that pulls this file shouldn't fail at import time just because the
# tray has no av installed. Functions reference ``av`` as a normal
# attribute access; the first such call triggers the import (Python
# caches the module afterwards).
class _LazyAv:
    _module = None

    def __getattr__(self, name):
        if _LazyAv._module is None:
            import av as _av_module

            _LazyAv._module = _av_module
        return getattr(_LazyAv._module, name)


av = _LazyAv()


logger = logging.getLogger(__name__)

# Default timeout for FFmpeg operations (30 minutes).
# Long videos (2+ hours of 180-degree footage) can take a while to process.
FFMPEG_TIMEOUT = 1800

# Per-I/O timeout passed to libav via PyAV's interrupt_callback. This is the
# only hang-protection that actually works for av.open — asyncio.wait_for on a
# thread can't kill the underlying C call since Python threads are unkillable.
AV_IO_TIMEOUT = 30.0

# Audio/video sync guards for the combine step (see _combine_copy and
# detect_audio_video_gaps). Cameras occasionally drop audio for part of a
# recording (e.g. a Reolink mic glitch that ends the audio track ~12s before
# the video). Left alone, the combine packs each segment's audio contiguously
# while video gets a per-segment offset, so one short segment shifts every
# later segment's audio earlier than its video and the whole game drifts.
#
# _AUDIO_PAD_EPSILON_SECONDS: pad a segment's audio with silence once it is at
# least this much shorter than its own video. Small enough to correct real
# dropouts, large enough to ignore normal AAC framing slack (~0.06s/segment).
_AUDIO_PAD_EPSILON_SECONDS = 0.5
# AUDIO_GAP_WARN_THRESHOLD_SECONDS: surface an NTFY warning to the camera
# manager when this much game audio is missing — a genuinely lost stretch, not
# encoder framing. Padding still happens regardless; this only gates the alert.
AUDIO_GAP_WARN_THRESHOLD_SECONDS = 3.0

# Decode-probe tuning. PR #88 guarantees every downloaded segment is
# byte-complete (size == camera-reported), but a byte-complete file can still
# carry a corrupt HEVC region the camera wrote to its own SD card: it decodes
# fine for minutes, then avcodec_send_packet raises InvalidDataError and the
# rest of the segment is undecodable. A size check can't see this and a
# stream-copy combine can't either (a pure mux never calls the decoder, so the
# garbage packets get muxed straight through). Only forcing a real decode trips
# on it — see decode_probe / detect_video_decode_corruption below.
#
# DECODE_PROBE_WINDOW_SECONDS: how many seconds to actually decode per sampled
# window. Big enough to force avcodec_send_packet over a meaningful run of
# packets, small enough that probing a multi-hour recording stays a few seconds.
DECODE_PROBE_WINDOW_SECONDS = 3.0
# DECODE_PROBE_STEP_SECONDS: spacing between sampled windows when scanning a
# whole segment for the first decode failure. The 06.08 corruption sat ~287s
# into a 300s file, so a coarse sweep across the segment (not just first/mid/
# last) is what localizes the dead region cheaply.
DECODE_PROBE_STEP_SECONDS = 30.0

# numpy dtype (as a string) to allocate silence for each libav sample format.
_SILENCE_DTYPE = {
    "dbl": "float64",
    "dblp": "float64",
    "flt": "float32",
    "fltp": "float32",
    "s16": "int16",
    "s16p": "int16",
    "s32": "int32",
    "s32p": "int32",
    "u8": "uint8",
    "u8p": "uint8",
}

_EXT_TO_AV_FORMAT = {
    ".mp4": "mp4",
    ".m4v": "mp4",
    ".mov": "mp4",
    ".mkv": "matroska",
}


def _av_format_for(path: str) -> str | None:
    return _EXT_TO_AV_FORMAT.get(os.path.splitext(path)[1].lower())


def av_open_read(
    path: str,
    *,
    format: str | None = None,
    timeout: float = AV_IO_TIMEOUT,
    **kwargs,
):
    """Open a container for reading with a format hint and libav I/O timeout.

    Every read site in this codebase goes through here. The ``format`` hint
    skips libav's probe step, which has been observed to hang in the frozen
    Windows service on large inputs. The ``timeout`` routes through libav's
    interrupt_callback so a hung C-level read is actually interruptible —
    unlike ``asyncio.wait_for`` wrapping a thread, which can't kill the thread.
    """
    fmt = format if format is not None else _av_format_for(path)
    return av.open(path, format=fmt, timeout=timeout, **kwargs)


def av_open_write(
    path: str,
    *,
    format: str = "mp4",
    timeout: float = AV_IO_TIMEOUT,
    **kwargs,
):
    """Open a container for writing with an explicit format.

    Every write site in this codebase goes through here. PyInstaller-frozen
    bundles can't infer output format from the extension, so we always pass
    one explicitly (defaults to mp4, which covers every current call site).
    """
    return av.open(path, "w", format=format, timeout=timeout, **kwargs)


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


def _get_video_duration_sync(file_path: str) -> float | None:
    """Synchronous implementation: get video duration via av.open metadata."""
    with av_open_read(file_path) as container:
        if container.duration is not None:
            return container.duration / av.time_base
        # Fallback: check the first video stream's duration
        for stream in container.streams.video:
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
    return None


async def get_video_duration(file_path: str) -> float | None:
    """Get the duration of a video file using PyAV."""
    try:
        return await asyncio.to_thread(_get_video_duration_sync, file_path)
    except Exception as e:
        logger.error(f"Error getting video duration: {e}")
        return None


def _decode_window_sync(
    container, video_stream, start_sec: float, window_seconds: float
) -> None:
    """Decode ~``window_seconds`` of *video_stream* starting at *start_sec*.

    Seeks to the window start and decodes packets, which is where libav calls
    ``avcodec_send_packet`` — the exact call that raises on a corrupt HEVC
    packet that size-only validation can't see (demux alone never decodes, so
    it never trips). Raises ``av.error.FFmpegError`` / ``av.error.Invalid
    DataError`` on corruption; returns normally when the window decodes clean.
    """
    time_base = video_stream.time_base
    if start_sec > 0 and time_base:
        container.seek(int(start_sec / time_base), stream=video_stream)

    first_pts_sec = None
    for packet in container.demux(video_stream):
        for frame in packet.decode():
            if frame.pts is not None and time_base:
                frame_sec = float(frame.pts * time_base)
                if first_pts_sec is None:
                    first_pts_sec = frame_sec
                elif frame_sec - first_pts_sec >= window_seconds:
                    return


def _window_decodes_clean(
    container, video_stream, start_sec: float, window_seconds: float
) -> bool:
    """True if a ``window_seconds`` window from *start_sec* decodes without error."""
    try:
        _decode_window_sync(container, video_stream, start_sec, window_seconds)
        return True
    except (av.error.InvalidDataError, av.error.FFmpegError):
        return False


def _decode_probe_sync(file_path: str, window_seconds: float) -> float | None:
    """Synchronous implementation of :func:`decode_probe`.

    Two passes, so the whole file isn't re-decoded yet the cut point is precise:

    1. *Coarse sweep* — decode a window at 0, then every
       ``DECODE_PROBE_STEP_SECONDS``, then the tail, until one fails.
    2. *Refine* — walk forward in ``window_seconds`` steps from the last clean
       window to the failing one to localize the FIRST undecodable second.

    Returns ``None`` if every sampled window decodes cleanly, else the refined
    second where decoding first fails — the start of the undecodable region, so
    the combine cuts there and drops the dead tail (not a window-rounded
    over-cut that would leave bad packets straddling the join).
    """
    with av_open_read(file_path) as container:
        if not container.streams.video:
            logger.warning("DECODE_PROBE: no video stream in %s", file_path)
            return 0.0
        video_stream = container.streams.video[0]

        duration = None
        time_base = video_stream.time_base
        if video_stream.duration is not None and time_base:
            duration = float(video_stream.duration * time_base)
        elif container.duration is not None:
            duration = container.duration / av.time_base

        # Coarse sweep: 0, body @ STEP spacing, then the tail.
        window_starts = [0.0]
        if duration is not None and duration > window_seconds:
            t = DECODE_PROBE_STEP_SECONDS
            while t < duration - window_seconds:
                window_starts.append(t)
                t += DECODE_PROBE_STEP_SECONDS
            window_starts.append(max(0.0, duration - window_seconds))
        window_starts = sorted(set(window_starts))

        last_clean = 0.0
        coarse_fail = None
        for start_sec in window_starts:
            if _window_decodes_clean(
                container, video_stream, start_sec, window_seconds
            ):
                last_clean = start_sec + window_seconds
            else:
                coarse_fail = start_sec
                break

        if coarse_fail is None:
            return None

        # Refine: walk window-by-window from the last clean point up to the
        # coarse failure to find the first failing second precisely.
        refine = last_clean
        while refine < coarse_fail:
            if _window_decodes_clean(container, video_stream, refine, window_seconds):
                refine += window_seconds
            else:
                break

        first_bad = refine if refine <= coarse_fail else coarse_fail
        logger.warning(
            "DECODE_PROBE: %s first undecodable region near %.1fs",
            os.path.basename(file_path),
            first_bad,
        )
        return first_bad


async def decode_probe(
    file_path: str, window_seconds: float = DECODE_PROBE_WINDOW_SECONDS
) -> float | None:
    """Probe *file_path* for an undecodable region by forcing a real decode.

    Size checks can't see in-file corruption: a camera segment can download at
    EXACTLY the reported byte count (so PR #88's completeness check passes) yet
    carry a corrupt HEVC packet the camera wrote to its SD card, which explodes
    the decoder (``InvalidDataError`` from ``avcodec_send_packet``) only when
    something actually decodes the stream — combine/trim stream-copy never does,
    so the garbage rides straight through into the shipped video.

    This samples windows across the file and forces a decode over each, which is
    cheap (no full re-decode) but reliably trips on the bad packet.

    Returns ``None`` when every sampled window decodes cleanly, or the
    approximate second where the first decode failure starts (so the combine can
    cut from there). A missing-file / missing-stream case returns ``0.0`` (whole
    segment is unusable).
    """
    if not os.path.exists(file_path):
        logger.error("DECODE_PROBE: file not found: %s", file_path)
        return 0.0
    try:
        return await _run_in_thread_with_timeout(
            _decode_probe_sync, file_path, window_seconds, timeout=120
        )
    except (av.error.InvalidDataError, av.error.FFmpegError) as e:
        logger.warning(
            "DECODE_PROBE: %s failed decode probe: %s",
            os.path.basename(file_path),
            e,
        )
        return 0.0
    except TimeoutError:
        logger.error("DECODE_PROBE: timed out probing %s", os.path.basename(file_path))
        return None
    except Exception as e:
        logger.error(
            "DECODE_PROBE: unexpected error probing %s: %s",
            os.path.basename(file_path),
            e,
        )
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
    with av_open_read(file_path) as input_container:
        with av_open_write(
            output_path, options={"movflags": "faststart"}
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


async def async_convert_file(file_path: str) -> str | None:
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
    except TimeoutError:
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
    video_path: str,
    output_path: str,
    time_offset: str,
    max_dim: int | None = None,
    quality: int = 95,
) -> bool:
    """Synchronous implementation: extract a single frame as JPEG.

    For HEVC stream-copy files, seeking can land between keyframes and produce
    corrupt (green) frames.  Strategy: try the requested offset first, then
    fall back to progressively later positions until a clean frame is found.

    ``max_dim`` (longest-side pixels) and ``quality`` (1-100) let callers
    cap the output size — the NTFY notification path uses 1280/75 to stay
    under ntfy.sh's ~4 MB free-tier attachment cap. The default leaves
    full-resolution / quality-95 output unchanged for everything else.
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

    with av_open_read(video_path) as container:
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
                    if max_dim:
                        image.thumbnail(
                            (max_dim, max_dim),
                            resample=image.Resampling.LANCZOS
                            if hasattr(image, "Resampling")
                            else 1,
                        )
                    image.save(output_path, "JPEG", quality=quality, optimize=True)
                    return True

        # All attempts failed
        logger.warning(
            f"Could not find a non-corrupt frame in {os.path.basename(video_path)}"
        )
        return False


async def create_screenshot(
    video_path: str,
    output_path: str,
    time_offset: str = "00:00:01",
    max_dim: int | None = None,
    quality: int = 95,
) -> bool:
    """Creates a screenshot from a video file.

    Pass ``max_dim``/``quality`` to produce a smaller JPEG (NTFY
    notifications need to stay under ntfy.sh's free-tier attachment
    cap of ~4 MB; 4K Reolink panoramas at quality 95 routinely
    exceed that).
    """
    try:
        result = await _run_in_thread_with_timeout(
            _create_screenshot_sync,
            video_path,
            output_path,
            time_offset,
            max_dim,
            quality,
            timeout=60,
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
    input_path: str, output_path: str, start_offset: str, duration: str | None
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

    with av_open_read(input_path) as input_container:
        with av_open_write(
            output_path, options={"movflags": "faststart"}
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
    input_path: str, output_path: str, start_offset: str, duration: str | None = None
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
    except TimeoutError:
        logger.error(f"Trim timed out after {FFMPEG_TIMEOUT}s")
        _cleanup_temp(temp_output)
        return False
    except Exception as e:
        logger.error(f"Error during trim: {e}")
        _cleanup_temp(temp_output)
        return False


def _encode_silence(
    out_audio_stream, output_container, template_frame, seconds: float
) -> None:
    """Encode ``seconds`` of silence into ``out_audio_stream`` and mux it.

    Silence frames are built in the same sample format / layout / rate as a
    previously-decoded real frame (``template_frame``) so the AAC encoder
    accepts them on the exact path it already accepted the camera audio —
    no resampling or layout surprises.
    """
    import numpy as np

    rate = template_frame.sample_rate
    if not rate or seconds <= 0:
        return
    fmt = template_frame.format
    layout = template_frame.layout
    dtype = _SILENCE_DTYPE.get(fmt.name, "float32")
    nchannels = len(layout.channels)
    total_samples = int(round(seconds * rate))
    chunk = 1024
    written = 0
    while written < total_samples:
        n = min(chunk, total_samples - written)
        if fmt.is_planar:
            arr = np.zeros((nchannels, n), dtype=dtype)
        else:
            arr = np.zeros((1, n * nchannels), dtype=dtype)
        silence = av.AudioFrame.from_ndarray(arr, format=fmt.name, layout=layout.name)
        silence.sample_rate = rate
        silence.pts = None
        for out_packet in out_audio_stream.encode(silence):
            output_container.mux(out_packet)
        written += n


def detect_audio_video_gaps(
    file_paths: list[str],
    threshold_seconds: float = AUDIO_GAP_WARN_THRESHOLD_SECONDS,
) -> list[dict]:
    """Return source segments whose audio length materially differs from video.

    Each entry is ``{"path", "video_seconds", "audio_seconds", "gap_seconds",
    "kind"}`` where ``gap_seconds`` is the absolute mismatch and ``kind`` is
    ``"short"`` (audio shorter than video) or ``"long"`` (audio longer). A
    segment with no audio stream reports the full video length as a short gap.

    This is the user-facing signal: ``_combine_copy`` pads short audio with
    silence and trims long audio so playback stays in sync, but the camera
    manager still wants to know a segment's audio didn't match its video
    (almost always a camera glitch).
    """
    gaps: list[dict] = []
    for path in file_paths:
        try:
            with av_open_read(path) as container:
                video = next((s for s in container.streams if s.type == "video"), None)
                audio = next((s for s in container.streams if s.type == "audio"), None)
                if video is None:
                    continue
                if video.duration:
                    video_seconds = float(video.duration * video.time_base)
                elif container.duration:
                    video_seconds = container.duration / 1_000_000
                else:
                    continue
                if audio is not None and audio.duration:
                    audio_seconds = float(audio.duration * audio.time_base)
                else:
                    audio_seconds = 0.0
                gap = video_seconds - audio_seconds
                if abs(gap) > threshold_seconds:
                    gaps.append(
                        {
                            "path": path,
                            "video_seconds": video_seconds,
                            "audio_seconds": audio_seconds,
                            "gap_seconds": abs(gap),
                            "kind": "short" if gap > 0 else "long",
                        }
                    )
        except Exception as exc:
            logger.warning(f"COMBINE: audio-gap probe failed for {path}: {exc}")
    return gaps


def detect_video_decode_corruption(
    file_paths: list[str],
    window_seconds: float = DECODE_PROBE_WINDOW_SECONDS,
) -> list[dict]:
    """Return source segments that decode partway then hit a corrupt region.

    The sibling of :func:`detect_audio_video_gaps` for the case audio/video
    *duration* matching can't catch: the 06.08 segment decoded fine to 287.07s,
    then ``avcodec_send_packet`` raised and ~13s was undecodable, yet the
    container's *duration* still read the full 300s (the corrupt packets carry
    PTS), so the audio-gap detector saw nothing wrong. Only a decode probe trips
    on it.

    Each entry is ``{"path", "corrupt_start_seconds", "video_seconds",
    "lost_seconds"}`` where ``corrupt_start_seconds`` is where decoding first
    failed (the cut point), ``video_seconds`` the segment's container duration,
    and ``lost_seconds`` the trailing span that gets removed by the combine
    degrade path. ``_combine_copy`` consumes ``corrupt_start_seconds`` to drop
    the dead video region; ``VideoProcessor`` surfaces the list as an NTFY
    warning and flags the game so it isn't shipped as if perfect.
    """
    corruptions: list[dict] = []
    for path in file_paths:
        try:
            corrupt_start = _decode_probe_sync(path, window_seconds)
        except Exception as exc:
            logger.warning(f"COMBINE: decode probe failed for {path}: {exc}")
            continue
        if corrupt_start is None:
            continue
        video_seconds = corrupt_start
        try:
            dur = _get_video_duration_sync(path)
            if dur is not None:
                video_seconds = dur
        except Exception:
            pass
        lost_seconds = max(0.0, video_seconds - corrupt_start)
        corruptions.append(
            {
                "path": path,
                "corrupt_start_seconds": corrupt_start,
                "video_seconds": video_seconds,
                "lost_seconds": lost_seconds,
            }
        )
    return corruptions


def _combine_videos_sync(
    file_paths: list[str],
    output_path: str,
    camera_name: str | None = None,
    camera_type: str | None = None,
    corrupt_starts: dict[str, float] | None = None,
) -> bool:
    """Synchronous implementation: concatenate multiple video files (video copy, AAC re-encode).

    Uses stream copy for video regardless of codec (H.264, HEVC, etc.) for speed.
    HEVC-to-H.264 transcoding is deferred to later pipeline stages (trim/autocam)
    where the video is shorter and transcoding is practical.

    ``corrupt_starts`` maps a segment path to the second where its video becomes
    undecodable (from :func:`detect_video_decode_corruption`). The combine cuts
    each such segment's video at that point so the dead region is dropped instead
    of muxed as garbage; the audio-align step then trims that segment's audio to
    the shortened video, keeping A/V in sync with the corrupt span removed.
    """
    if not file_paths:
        raise ValueError("No files to combine")

    with av_open_write(
        output_path, options={"movflags": "faststart"}
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
        with av_open_read(file_paths[0]) as probe:
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
            file_paths,
            output_container,
            out_video_stream,
            out_audio_stream,
            corrupt_starts or {},
        )


def _combine_copy(
    file_paths: list[str],
    output_container,
    out_video_stream,
    out_audio_stream,
    corrupt_starts: dict[str, float] | None = None,
) -> bool:
    """Concatenate files using stream copy (fast path for H.264 input).

    Video packets are stream-copied with a per-segment PTS offset. Audio is
    decoded and re-encoded contiguously, then — crucially — each segment's
    audio is topped up with silence to match that segment's own video duration
    before moving to the next file. Without that pad, a segment whose camera
    audio is short (e.g. the mic dropped for the last ~12s of a 5-minute file)
    would leave its audio track short while video advanced the full length, so
    every later segment's audio would land earlier than its video and the whole
    game would drift out of sync. Padding confines a camera audio gap to
    trailing silence inside the one bad segment.

    The mirror case is handled too: if a segment's audio overruns its video
    (audio longer than video), the excess audio is trimmed so it can't push
    later segments' audio ahead of their video. ``detect_audio_video_gaps``
    surfaces either condition to the user as an NTFY warning.

    ``corrupt_starts`` (path -> second) marks segments with an undecodable
    region (from :func:`detect_video_decode_corruption`). For such a segment we
    DEGRADE rather than mux garbage: video packets at/after the corrupt second
    are dropped, so the dead span is cut. The per-segment audio target is
    shrunk to the cut point too, so the existing audio-trim/pad logic re-aligns
    that segment's audio to its now-shorter video — A/V stays in sync with the
    corrupt region removed.
    """
    corrupt_starts = corrupt_starts or {}
    video_pts_offset = 0
    # A decoded real frame, reused as the format/layout/rate template for any
    # silence we synthesize. Captured from the first segment that has audio.
    audio_template = None

    for file_path in file_paths:
        # If this segment was flagged corrupt, cut its video at the first
        # undecodable second instead of muxing the garbage that follows.
        seg_cut_s = corrupt_starts.get(file_path)
        with av_open_read(file_path) as input_container:
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
            seg_audio_samples = 0
            seg_audio_rate = 0
            # Metadata video duration for this segment. Needed to trim
            # overrunning audio mid-stream, since the decode-accurate
            # ``max_video_pts`` isn't known until after the demux loop.
            seg_video_target_s = None
            if in_video is not None and in_video.duration:
                seg_video_target_s = float(in_video.duration * in_video.time_base)
            # A corrupt segment's metadata duration still reads the full length
            # (the bad packets carry PTS), so clamp the audio target to the cut
            # point — otherwise audio would be trimmed/padded to dead video.
            if seg_cut_s is not None:
                seg_video_target_s = (
                    seg_cut_s
                    if seg_video_target_s is None
                    else min(seg_video_target_s, seg_cut_s)
                )
                logger.warning(
                    "COMBINE: cutting corrupt video region in %s at %.1fs "
                    "(dropping the undecodable tail)",
                    os.path.basename(file_path),
                    seg_cut_s,
                )

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

                        # Cut the dead region: once this corrupt segment reaches
                        # the undecodable second, stop muxing its video so the
                        # garbage tail never lands in the output.
                        if (
                            seg_cut_s is not None
                            and in_video.time_base
                            and packet.pts is not None
                            and float(packet.pts * in_video.time_base) >= seg_cut_s
                        ):
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
                            if audio_template is None:
                                audio_template = frame
                            if frame.sample_rate:
                                seg_audio_rate = frame.sample_rate
                            # Trim: once this segment's audio already covers its
                            # video (plus tolerance), drop the rest so a too-long
                            # audio track can't push later segments' audio ahead
                            # of their video.
                            if (
                                seg_video_target_s is not None
                                and seg_audio_rate
                                and seg_audio_samples / seg_audio_rate
                                > seg_video_target_s + _AUDIO_PAD_EPSILON_SECONDS
                            ):
                                continue
                            seg_audio_samples += frame.samples
                            frame.pts = None
                            for out_packet in out_audio_stream.encode(frame):
                                output_container.mux(out_packet)
                except (av.InvalidDataError, av.error.FFmpegError):
                    continue

            # Keep cumulative audio aligned with cumulative video: pad this
            # segment's audio with silence up to its own video duration. Only
            # acts on a meaningful shortfall (> epsilon) and only once we have a
            # template frame to synthesize silence from.
            if out_audio_stream is not None and in_video is not None and seg_audio_rate:
                seg_video_seconds = float(max_video_pts * in_video.time_base)
                seg_audio_seconds = seg_audio_samples / seg_audio_rate
                deficit = seg_video_seconds - seg_audio_seconds
                if deficit > _AUDIO_PAD_EPSILON_SECONDS and audio_template is not None:
                    logger.info(
                        "COMBINE: padding %.1fs of silence for %s "
                        "(audio %.1fs < video %.1fs)",
                        deficit,
                        os.path.basename(file_path),
                        seg_audio_seconds,
                        seg_video_seconds,
                    )
                    _encode_silence(
                        out_audio_stream, output_container, audio_template, deficit
                    )

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
    corrupt_starts: dict[str, float] | None = None,
) -> bool:
    """Combines multiple video files into a single MP4 using PyAV.

    Writes to a temp file first, then renames on success so a crash
    never leaves a partial MP4 at the final path.

    Args:
        file_paths: List of input file paths to concatenate.
        output_path: Path for the combined output file.
        camera_name: Optional camera name to embed in MP4 metadata.
        camera_type: Optional camera type to embed in MP4 metadata.
        corrupt_starts: Optional map of segment path -> second where its video
            becomes undecodable (from :func:`detect_video_decode_corruption`).
            Each such segment's video is cut at that point so the dead region is
            dropped rather than muxed as garbage.
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
            corrupt_starts,
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
    except TimeoutError:
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
    with av_open_read(input_path) as input_container:
        with av_open_write(output_path) as output_container:
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
    with av_open_read(input_path) as input_container:
        with av_open_write(output_path) as output_container:
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
    with av_open_write(output_path) as output_container:
        out_video = None
        out_audio = None

        # Probe first clip for stream parameters
        with av_open_read(clip_paths[0]) as probe:
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
            with av_open_read(clip_path) as input_container:
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
