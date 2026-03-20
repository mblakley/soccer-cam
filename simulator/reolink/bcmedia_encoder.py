"""BcMedia encoder for Baichuan protocol streaming.

Converts stored video files to BcMedia binary format for streaming
over the Baichuan TCP protocol. The client (reolink_download.py)
expects unencrypted BcMedia frames with I-frame/P-frame magic numbers.

BcMedia format (all little-endian u32):
  INFO header: magic(4) + header_size(4) + width(4) + height(4) + ...
  Video frame: magic(4) + codec_ascii(4) + payload_size(4) + addl_header_size(4)
               + microseconds(4) + reserved(4) + NAL_data + padding_to_8
"""

import logging
import struct
from typing import Iterator

logger = logging.getLogger(__name__)

# BcMedia magic numbers (little-endian u32)
MAGIC_INFO_V1 = 0x31303031
MAGIC_IFRAME = 0x63643030
MAGIC_PFRAME = 0x63643130

PAD_SIZE = 8
ANNEX_B_START_CODE = b"\x00\x00\x00\x01"


def _pad_to_8(size: int) -> int:
    """Calculate padding needed to align to 8 bytes."""
    return 0 if size % PAD_SIZE == 0 else PAD_SIZE - (size % PAD_SIZE)


def encode_info_header(width: int, height: int, fps: int) -> bytes:
    """Build a 32-byte BcMedia info header (MAGIC_INFO_V1)."""
    buf = bytearray(32)
    struct.pack_into("<I", buf, 0, MAGIC_INFO_V1)
    struct.pack_into("<I", buf, 4, 32)  # header_size
    struct.pack_into("<I", buf, 8, width)
    struct.pack_into("<I", buf, 12, height)
    buf[17] = fps & 0xFF
    return bytes(buf)


def encode_video_frame(
    data: bytes, is_iframe: bool, codec: str, microseconds: int
) -> bytes:
    """Encode a single video frame in BcMedia format.

    Args:
        data: NAL unit data (Annex-B with start codes)
        is_iframe: True for I-frame, False for P-frame
        codec: "H264" or "H265"
        microseconds: Timestamp in microseconds

    Returns:
        BcMedia-framed bytes ready for TCP streaming.
    """
    magic = MAGIC_IFRAME if is_iframe else MAGIC_PFRAME
    codec_bytes = codec.encode("ascii")[:4].ljust(4, b"\x00")
    payload_size = len(data)
    addl_header_size = 0
    pad = _pad_to_8(payload_size)

    header = (
        struct.pack("<I", magic)
        + codec_bytes
        + struct.pack("<I", payload_size)
        + struct.pack("<I", addl_header_size)
        + struct.pack("<I", microseconds & 0xFFFFFFFF)
        + struct.pack("<I", 0)  # reserved
    )

    return header + data + (b"\x00" * pad)


def _is_hevc_nal(data: bytes, offset: int) -> bool:
    """Check if the NAL unit at offset is HEVC (H.265)."""
    if offset + 5 > len(data):
        return False
    nal_type = (data[offset + 4] >> 1) & 0x3F
    # HEVC NAL types 32-34 are VPS/SPS/PPS
    return nal_type in (32, 33, 34)


def _detect_codec(data: bytes) -> str:
    """Detect video codec from first few NAL units."""
    for i in range(min(len(data) - 5, 256)):
        if data[i : i + 4] == ANNEX_B_START_CODE:
            if _is_hevc_nal(data, i):
                return "H265"
    return "H264"


def encode_file_to_bcmedia(file_path: str) -> Iterator[bytes]:
    """Convert a video file to BcMedia binary chunks for streaming.

    Uses PyAV to demux the file and yield BcMedia-encoded frames.
    The caller should send these chunks over the TCP connection.

    Yields:
        bytes: BcMedia-encoded chunks (info header, then video frames).
    """
    import av

    try:
        container = av.open(file_path)
    except Exception as e:
        logger.error(f"Failed to open video file {file_path}: {e}")
        return

    try:
        video_stream = container.streams.video[0]
    except (IndexError, av.error.InvalidDataError):
        logger.error(f"No video stream found in {file_path}")
        container.close()
        return

    width = video_stream.codec_context.width or 1920
    height = video_stream.codec_context.height or 1080
    fps = int(video_stream.average_rate or 25)

    # Detect codec from stream metadata
    codec_name = video_stream.codec_context.name or ""
    if "hevc" in codec_name or "h265" in codec_name or "265" in codec_name:
        codec = "H265"
    else:
        codec = "H264"

    logger.info(f"BcMedia encoding: {width}x{height} @ {fps}fps, codec={codec}")

    # Send info header first
    yield encode_info_header(width, height, fps)

    # Stream packets as BcMedia frames
    frame_idx = 0
    time_base = float(video_stream.time_base) if video_stream.time_base else 1.0 / fps

    for packet in container.demux(video_stream):
        if packet.size == 0:
            continue

        # Calculate microsecond timestamp
        if packet.pts is not None:
            microseconds = int(packet.pts * time_base * 1_000_000)
        else:
            microseconds = int(frame_idx * (1_000_000 / fps))

        is_iframe = packet.is_keyframe
        nal_data = bytes(packet)

        yield encode_video_frame(nal_data, is_iframe, codec, microseconds)
        frame_idx += 1

    container.close()
    logger.info(f"BcMedia encoding complete: {frame_idx} frames")
