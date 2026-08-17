"""Stitch-correction step — apply per-row dx shift to fix the dual-lens seam.

Reads the panoramic ``input_path``, applies
:func:`video_grouper.utils.stitch_remap.apply_shift_to_frame_rgb` per frame
using the configured calibration profile, writes a corrected mp4 alongside, and
rebinds ``input_path`` so downstream steps consume the corrected video. The
re-encode mirrors the source stream's codec, bitrate, pixel format, frame rate
and colour metadata — see ``_output_stream_spec``.

Pass-through (no work, no error) when the profile path isn't configured or the
file isn't loadable — stitch correction is an opt-in calibration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel

from video_grouper.pipeline import register_step
from video_grouper.pipeline.base import PipelineStep, StepContext
from video_grouper.pipeline.manifest import PipelineManifest

if TYPE_CHECKING:
    # Type-only: av is imported lazily inside the helpers below so this module
    # stays importable where av isn't installed.
    from av.container import OutputContainer
    from av.video.stream import VideoStream

logger = logging.getLogger(__name__)

# This step is FIRST in the homegrown preset chain (see pipeline/presets.py), so
# whatever it throws away is thrown away for the detector, tracker and renderer
# too. The camera's daily-driver encode is 7680x2160 H.265 at 20 fps / 20480
# kbps — its hardware ceiling (reolink-firmware-patching/docs/PATCHING_GUIDE.md
# step 7). Re-encoding to h264 with no bit_rate set hands the whole panorama to
# PyAV's default rate control (~1 Mbps at any resolution), an order-of-magnitude
# cut that would cost more than the seam repair gains. So: carry the source's
# codec, bitrate, pixel format, frame rate, time base and colour metadata.
_FALLBACK_CODEC = "h264"

# Bits per pixel per frame, used ONLY when neither the stream nor the container
# declares a usable bitrate. Derived from the camera's own ceiling encode:
# 20_480_000 bps / (7680 * 2160 px * 20 fps) = 0.0617 bpp. Scaling that by the
# real frame size and rate reproduces the source's own quality target at any
# resolution — a number we can point at, rather than PyAV's silent default.
_FALLBACK_BITS_PER_PIXEL = 20_480_000 / (7680 * 2160 * 20)

# Only reached if the source reports no frame rate either. 20 fps is the Duo 3's
# real hardware ceiling (reolink-firmware-patching/docs/FIRMWARE_PATCH_NOTES.md).
_FALLBACK_FPS = 20.0


class StitchCorrectStepConfig(BaseModel):
    stitch_profile_path: str | None = None


@dataclass(frozen=True)
class _OutputStreamSpec:
    """Encoder settings for the corrected copy, taken from the source stream."""

    codec_name: str
    width: int
    height: int
    pix_fmt: str
    bit_rate: int
    # PyAV-typed passthroughs: Fraction rate/time_base and the colour enums.
    rate: Any = None
    time_base: Any = None
    color_range: Any = None
    colorspace: Any = None
    color_primaries: Any = None
    color_trc: Any = None


def _resolve_encoder(codec_name: str) -> tuple[str, frozenset[str]]:
    """Return ``(encoder name, pixel formats it accepts)`` for ``codec_name``.

    Not every decodable codec has an *encoder* in the installed FFmpeg build, so
    fall back to h264 (loudly) rather than raising. ``av.Codec("hevc", "w")``
    resolves to libx265, so the camera's H.265 round-trips as H.265.
    """
    import av  # lazy: PyAV is heavy

    for candidate in (codec_name, _FALLBACK_CODEC):
        try:
            codec = av.Codec(candidate, "w")
        except Exception:
            continue
        if candidate != codec_name:
            logger.warning(
                "stitch_correct: no encoder for source codec %r; re-encoding as %s",
                codec_name,
                candidate,
            )
        return candidate, frozenset(f.name for f in (codec.video_formats or ()))
    # Neither resolved — hand the name to add_stream and let FFmpeg say why.
    return _FALLBACK_CODEC, frozenset()


def _output_stream_spec(
    in_video: Any,
    container_bit_rate: int | None = None,
    encoder_lookup: Callable[[str], tuple[str, frozenset[str]]] = _resolve_encoder,
) -> _OutputStreamSpec:
    """Build the output stream settings from the input video stream.

    Everything is carried across from the source; the only substitutions are the
    documented fallbacks (unavailable encoder, unsupported pixel format, absent
    bitrate), each of which logs.
    """
    codec_context = in_video.codec_context
    codec_name, encoder_pix_fmts = encoder_lookup(
        getattr(codec_context, "name", None) or _FALLBACK_CODEC
    )

    width = int(in_video.width)
    height = int(in_video.height)
    rate = in_video.average_rate

    pix_fmt = getattr(codec_context, "pix_fmt", None) or "yuv420p"
    if encoder_pix_fmts and pix_fmt not in encoder_pix_fmts:
        logger.warning(
            "stitch_correct: encoder %s does not accept source pix_fmt %s; using yuv420p",
            codec_name,
            pix_fmt,
        )
        pix_fmt = "yuv420p"

    bit_rate = int(getattr(in_video, "bit_rate", None) or 0)
    source = "stream"
    if bit_rate <= 0:
        # Container bitrate covers audio too, but audio is ~0.5% of a 20 Mbps
        # video stream — far closer than the encoder default.
        bit_rate = int(container_bit_rate or 0)
        source = "container"
    if bit_rate <= 0:
        fps = float(rate) if rate else _FALLBACK_FPS
        bit_rate = int(round(width * height * fps * _FALLBACK_BITS_PER_PIXEL))
        source = "camera-derived default"
    logger.info(
        "stitch_correct: re-encoding %dx%d as %s @ %d bps (%s), pix_fmt %s",
        width,
        height,
        codec_name,
        bit_rate,
        source,
        pix_fmt,
    )

    return _OutputStreamSpec(
        codec_name=codec_name,
        width=width,
        height=height,
        pix_fmt=pix_fmt,
        bit_rate=bit_rate,
        rate=rate,
        time_base=getattr(in_video, "time_base", None),
        color_range=getattr(codec_context, "color_range", None),
        colorspace=getattr(codec_context, "colorspace", None),
        color_primaries=getattr(codec_context, "color_primaries", None),
        color_trc=getattr(codec_context, "color_trc", None),
    )


def _add_output_stream(
    out_container: OutputContainer, spec: _OutputStreamSpec
) -> VideoStream:
    """Add and configure the output video stream described by ``spec``."""
    # add_stream is typed as the Stream union; a video codec name always yields
    # a VideoStream (same narrowing render.py does for the audio side).
    stream = cast(
        "VideoStream", out_container.add_stream(spec.codec_name, rate=spec.rate)
    )
    stream.width = spec.width
    stream.height = spec.height
    stream.pix_fmt = spec.pix_fmt
    codec_context = stream.codec_context
    codec_context.bit_rate = spec.bit_rate
    if spec.time_base is not None:
        # Match the source time_base. PyAV's default mis-budgets rate control
        # and corrupts duration metadata (see the same fix in steps/render.py).
        codec_context.time_base = spec.time_base
    # Carry colour metadata so the corrected copy is interpreted exactly as the
    # source was; an untagged re-encode is what makes a re-mux look shifted.
    for attr in ("color_range", "colorspace", "color_primaries", "color_trc"):
        value = getattr(spec, attr)
        if value is not None:
            setattr(codec_context, attr, value)
    return stream


def _correct_video(input_path: str, output_path: str, profile_path: str) -> bool:
    """Sync helper: read input, apply per-row dx shift, write output.

    Returns True on success, False on any failure (caller decides what to do).
    """
    import av  # lazy: PyAV is heavy

    from video_grouper.utils.stitch_remap import (
        apply_shift_to_frame_rgb,
        build_dx_lookup,
        load_profile,
    )

    profile = load_profile(profile_path)
    if profile is None:
        logger.warning(
            "stitch_correct: profile not loadable at %s; skipping correction",
            profile_path,
        )
        return False

    try:
        with av.open(input_path) as in_container:
            in_video = in_container.streams.video[0]
            dx_lookup = build_dx_lookup(profile, in_video.width, in_video.height)
            seam_x = int(profile.seam_x * (in_video.width / profile.source_width))

            spec = _output_stream_spec(
                in_video, container_bit_rate=in_container.bit_rate
            )

            with av.open(output_path, mode="w") as out_container:
                out_video = _add_output_stream(out_container, spec)

                for frame in in_container.decode(in_video):
                    rgb = frame.to_ndarray(format="rgb24")
                    corrected = apply_shift_to_frame_rgb(rgb, dx_lookup, seam_x)
                    new_frame = av.VideoFrame.from_ndarray(corrected, format="rgb24")
                    new_frame.pts = frame.pts
                    for packet in out_video.encode(new_frame):
                        out_container.mux(packet)

                for packet in out_video.encode():
                    out_container.mux(packet)
        return True
    except Exception:
        logger.exception("stitch_correct: encoding failed for %s", input_path)
        return False


class StitchCorrectStep(PipelineStep[StitchCorrectStepConfig]):
    name = "stitch_correct"
    config_model = StitchCorrectStepConfig
    consumes = ("input_path",)
    # Optional step: no declared output the runner must validate. When it
    # corrects, it records stitched_path and rebinds input_path in the manifest
    # artifact map (persisted across stages so downstream + resume see it);
    # pass-through is a no-op.
    produces = ()
    runtime = "service"
    requires = ("av",)
    resources = ("ram_heavy",)

    async def run(self, manifest: PipelineManifest, ctx: StepContext) -> bool:
        profile_path = self.config.stitch_profile_path
        if not profile_path:
            logger.info(
                "stitch_correct: no stitch_profile_path configured; passing through"
            )
            return True

        # input_path is the immutable source the runner binds before run().
        in_path = Path(cast(str, manifest.get("input_path")))
        out_path = in_path.with_name(f"{in_path.stem}.stitched.mp4")

        success = await asyncio.to_thread(
            _correct_video, str(in_path), str(out_path), profile_path
        )
        if not success:
            logger.warning(
                "stitch_correct: correction failed; downstream steps will use "
                "the uncorrected source"
            )
            return True

        manifest.put("stitched_path", str(out_path))
        manifest.put("input_path", str(out_path))
        return True


register_step(StitchCorrectStep.name, StitchCorrectStep, StitchCorrectStepConfig)
