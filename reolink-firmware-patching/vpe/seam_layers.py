#!/usr/bin/env python3
"""The two sensors' contributions to the seam, as two separable layers.

WHAT THIS IS FOR. Every earlier view of the seam had to work from the *fused*
panorama, where the blend window is already a mixture of both sensors. There is
no second layer in a mixture, so blink/anaglyph/mirror could only ever draw
*extrapolations from the shoulders* -- honest, but inference. This module
returns the real pair, un-blended, so those views become exact.

WHAT A LAYER PAIR IS, AND WHY IT IS DEFINED THIS ABSTRACTLY.

    "a left layer, a right layer, and the panorama columns they correspond to"

That is the whole contract, and it is deliberately the *only* contract. Today
the pair arrives as two 128-px strips lifted out of one ISF buffer. Tomorrow it
may arrive as two 3840-px per-sensor frames from the vendor's own two-channel
snap. Those differ in every dimension that a naive implementation would have
hard-coded -- width, panorama origin, whether the two layers even cover the same
columns -- and differ in none that the consumer actually needs. So:

  * each layer carries its OWN panorama origin (`left_x0`, `right_x0`). For the
    strip source they are equal; for full frames they are 0 and 3776. Code that
    assumed one shared origin would be rewritten by the second source.
  * the overlap is *derived* (`LayerPair.overlap`), never assumed. It is the
    intersection of the two column ranges, which for strips is the whole thing
    and for full frames is a 128-px window inside two 3840-px images.
  * no caller may write 128 or 3776. They come off the descriptor. The only
    numbers in this file that are specific to the strip transport live in
    `capture()`, which is the one function whose job *is* that transport.

WHAT IS TRUE OF THE PAIR, AND WHY IT MATTERS FOR CALIBRATION. Both layers are
already resampled into the panorama's output coordinate frame -- the warp has
run, the stitcher has not. So the pair is pre-rectified, and an L-to-R
displacement measured on it is residual disparity in *output pixels*: it
converts to `dx_anchors` with no lens model, no homography and no rescale. That
is what makes hand-alignment on these two images a calibration rather than a
picture.

CAMERA ACCESS IS READ-ONLY. `capture()` pulls a frame descriptor and releases it
microseconds later, then reads physical memory. It never calls `SetStitch`,
never writes a mesh, never touches flash.
"""

from __future__ import annotations

import functools
import http.server
import re
import socketserver
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Transport constants -- specific to the strip source, used only by capture().
# --------------------------------------------------------------------------

#: VIDEOPROC 2's out[1] port. out[0] is the fused panorama and is contended;
#: out[1] carries the pre-blend pair and no other consumer pulls it.
ISF_ROOT_PATH = 0x00930000
ISF_PORT_PATH = 0x00930001

#: `arg[4]` of the pull ioctl is a **millisecond timeout**, not a flag word.
#: 200 ms is ~4 frame intervals at the port's 20 Hz, so it waits for a real
#: frame rather than racing the producer, and still bounds a stuck request.
PULL_TIMEOUT_MS = 200

#: The strip's Y plane: 256 x 2160, stride == width, linear. Also the dump
#: length, which /dev/mem requires to start 4 KB aligned.
STRIP_PACKED_W = 256
STRIP_H = 2160
STRIP_Y_BYTES = STRIP_PACKED_W * STRIP_H  # 0x87000

#: Where the strip sits in the panorama, measured: the blend band is columns
#: 3776..3903 and the cross-fade crosses over at 3840.
STRIP_PANO_X0 = 3776

DEFAULT_PANO_W = 7680
DEFAULT_PANO_H = 2160

CAM_TMP = "/mnt/tmp"


class LayerCaptureError(RuntimeError):
    """The pair could not be obtained. Always says which stage failed."""


# --------------------------------------------------------------------------
# The pair
# --------------------------------------------------------------------------


@dataclass
class LayerPair:
    """Two single-sensor views plus the panorama columns they occupy.

    `left` and `right` are 2-D uint8 luma. They need not be the same width and
    need not start at the same panorama column -- see the module docstring.
    """

    left: Any  # np.ndarray (h, wL) uint8
    right: Any  # np.ndarray (h, wR) uint8
    left_x0: int
    right_x0: int
    #: Panorama column where the cross-fade hands over from left to right.
    #: `None` means "the centre of the overlap", which is what it measured as
    #: (band 3776..3903, crossover 3840) and what it must be for a symmetric
    #: ramp. Deriving it rather than storing 3840 is what makes the same code
    #: correct for full per-sensor frames, whose overlap is the same 128
    #: columns sitting inside two 3840-px images.
    seam_x: int | None = None
    pano_w: int = DEFAULT_PANO_W
    pano_h: int = DEFAULT_PANO_H
    source: str = "unknown"
    detail: str = ""
    #: True when these are genuinely separated sensor layers rather than
    #: anything reconstructed. The UI promises exactness on the strength of
    #: this flag, so nothing sets it True speculatively.
    exact: bool = True
    #: Only the synthetic source fills this: the offset and roll it built in,
    #: so a test can assert the tool recovers what was hidden.
    truth: dict | None = None

    def __post_init__(self) -> None:
        if self.left.ndim != 2 or self.right.ndim != 2:
            raise LayerCaptureError("layers must be 2-D single-channel images")
        if self.left.shape[0] != self.right.shape[0]:
            raise LayerCaptureError(
                f"layers disagree on height: {self.left.shape[0]} vs "
                f"{self.right.shape[0]} -- they are not the same moment"
            )
        if self.seam_x is None:
            lo, hi = self.overlap
            if hi <= lo:
                raise LayerCaptureError(
                    f"the two layers do not overlap (left {self.left_x0}..{self.left_x1}, "
                    f"right {self.right_x0}..{self.right_x1}) -- there is no seam "
                    "between them to align"
                )
            self.seam_x = (lo + hi) // 2

    @property
    def height(self) -> int:
        return int(self.left.shape[0])

    @property
    def left_x1(self) -> int:
        return self.left_x0 + int(self.left.shape[1])

    @property
    def right_x1(self) -> int:
        return self.right_x0 + int(self.right.shape[1])

    @property
    def overlap(self) -> tuple[int, int]:
        """Panorama columns where BOTH layers have pixels. Derived, never fixed.

        This is the only region where a registration measure means anything,
        and it is the region the UI centres on. For the strip source it is the
        full 128 columns; for full per-sensor frames it is a narrow window
        inside two much larger images.
        """
        return (max(self.left_x0, self.right_x0), min(self.left_x1, self.right_x1))

    def to_api(self) -> dict:
        lo, hi = self.overlap
        return {
            "source": self.source,
            "detail": self.detail,
            "exact": bool(self.exact),
            # A LayerPair is by construction in panorama output coordinates --
            # that is the property that makes a drag on it a calibration rather
            # than a picture, so the UI asserts it before binding a gesture
            # instead of trusting a label. Sensor-space frames are SensorViews.
            "space": "panorama",
            "authoritative": True,
            "height": self.height,
            "seam_x": int(self.seam_x),
            "pano_w": int(self.pano_w),
            "pano_h": int(self.pano_h),
            "left": {
                "x0": int(self.left_x0),
                "x1": int(self.left_x1),
                "w": int(self.left.shape[1]),
            },
            "right": {
                "x0": int(self.right_x0),
                "x1": int(self.right_x1),
                "w": int(self.right.shape[1]),
            },
            "overlap": {"x0": int(lo), "x1": int(hi), "w": int(max(0, hi - lo))},
            "truth": self.truth,
        }

    def registration(self) -> dict:
        """Mean |L-R| and normalised cross-correlation over the overlap, as-is.

        The server-side twin of the number the UI recomputes on every drag.
        Reported at zero correction so an operator has a starting value to
        improve on, and so a test can check that the client and the server are
        measuring the same thing.
        """
        import numpy as np

        lo, hi = self.overlap
        if hi <= lo:
            return {"mad": None, "ncc": None, "n": 0}
        left = self.left[:, lo - self.left_x0 : hi - self.left_x0].astype(np.float64)
        right = self.right[:, lo - self.right_x0 : hi - self.right_x0].astype(
            np.float64
        )
        mad = float(np.abs(left - right).mean())
        la, ra = left - left.mean(), right - right.mean()
        den = float(np.sqrt((la * la).sum() * (ra * ra).sum()))
        ncc = None if den <= 0 else float((la * ra).sum() / den)
        return {"mad": mad, "ncc": ncc, "n": int(left.size)}


def split_packed(
    buf: bytes,
    *,
    width: int = STRIP_PACKED_W,
    height: int = STRIP_H,
    pano_x0: int = STRIP_PANO_X0,
    seam_x: int | None = None,
    source: str = "file",
    detail: str = "",
) -> LayerPair:
    """Split one packed buffer into its two halves.

    The strip transport hands back a single (height, width) plane whose left
    half is sensor 0's contribution and whose right half is sensor 1's, both
    covering the *same* panorama columns. That co-location is the surprising
    part and the reason this is a named function rather than a slice at the
    call site: `left_x0 == right_x0`, and a reader who assumed the two halves
    were adjacent in the panorama would be wrong by 128 px.
    """
    import numpy as np

    need = width * height
    if len(buf) < need:
        raise LayerCaptureError(
            f"expected at least {need} bytes of luma ({width}x{height}), got {len(buf)}"
        )
    plane = np.frombuffer(buf[:need], dtype=np.uint8).reshape(height, width)
    half = width // 2
    return LayerPair(
        left=np.ascontiguousarray(plane[:, :half]),
        right=np.ascontiguousarray(plane[:, half:]),
        left_x0=pano_x0,
        right_x0=pano_x0,
        seam_x=seam_x,
        source=source,
        detail=detail or f"{width}x{height} packed pair at panorama x={pano_x0}",
    )


def load_file(path: str | Path, **kw: Any) -> LayerPair:
    """Read an archived packed pair off disk.

    The fallback source, and the one that needs no camera: point it at a raw
    dump and the whole UI comes up. Geometry defaults to the strip's, and every
    part of it is overridable, because the next dump may not be 256 wide.
    """
    p = Path(path)
    if not p.is_file():
        raise LayerCaptureError(f"no such layer dump: {p}")
    kw.setdefault("detail", f"archived dump {p.name}")
    return split_packed(p.read_bytes(), source="file", **kw)


# --------------------------------------------------------------------------
# Synthetic source
# --------------------------------------------------------------------------


def synthetic(
    *,
    width: int = 128,
    height: int = STRIP_H,
    dx: float = 6.0,
    roll: float = 12.0,
    pano_x0: int = STRIP_PANO_X0,
    seed: int = 7,
) -> LayerPair:
    """A textured pair with a known offset and roll built in.

    This exists because the archived real pair *cannot* be aligned: it is a desk
    scene at 0.3 m where one sensor is defocused and blown out and the other
    sees near-black, with no shared texture -- a feature matcher gets zero
    keypoints on it. It proves the plumbing and the rendering and nothing else.
    Tuning an interaction against it, or quoting a disparity from it, would be
    reading noise.

    So the interaction is developed against this instead, where the answer is
    known: the right layer's content is displaced by

        d(y) = dx + roll * (y - mid) / (height - 1)

    and an operator (or a test) has succeeded when the authored curve reaches
    mean `dx` and top-to-bottom roll amplitude `roll`. Sign follows the shipped
    convention -- `dx` is px the RIGHT layer must move right.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    mid = (height - 1) / 2.0
    pad = int(np.ceil(abs(dx) + abs(roll))) + 4
    scene_w = width + 2 * pad

    # Band-limited noise: fine enough to make a 1-px error visible, coarse
    # enough to survive resampling. Plus hard vertical and diagonal edges,
    # which is what a human actually locks onto.
    coarse = rng.random((height // 8 + 2, scene_w // 8 + 2))
    scene = np.repeat(np.repeat(coarse, 8, axis=0), 8, axis=1)[:height, :scene_w]
    scene = 60.0 + 120.0 * scene
    xs = np.arange(scene_w)[None, :]
    ys = np.arange(height)[:, None]
    scene += 70.0 * (((xs // 17) % 2) == 0)  # vertical bars
    scene += 55.0 * ((((xs + ys // 3) // 23) % 2) == 0)  # slanted bars
    scene += rng.normal(0, 2.0, size=scene.shape)  # sensor noise, uncorrelated

    left = np.clip(scene[:, pad : pad + width], 0, 255).astype(np.uint8)

    # R samples the same scene displaced by d(y): content appears shifted LEFT,
    # so moving the right layer +d px right is the correction.
    d = dx + roll * (np.arange(height) - mid) / (height - 1)
    src_x = np.arange(width)[None, :] + pad + d[:, None]
    x0 = np.floor(src_x).astype(np.int64)
    frac = src_x - x0
    x0 = np.clip(x0, 0, scene_w - 2)
    row = np.arange(height)[:, None]
    right = scene[row, x0] * (1 - frac) + scene[row, x0 + 1] * frac
    right = np.clip(right + rng.normal(0, 2.0, size=right.shape), 0, 255).astype(
        np.uint8
    )

    return LayerPair(
        left=left,
        right=right,
        left_x0=pano_x0,
        right_x0=pano_x0,
        source="synthetic",
        detail=(
            f"test pair with a known answer: dx={dx:+.2f} px, "
            f"roll={roll:+.2f} px top-to-bottom"
        ),
        truth={"dx": float(dx), "roll": float(roll)},
    )


# --------------------------------------------------------------------------
# Live capture
# --------------------------------------------------------------------------


def _camsh() -> Any:
    """The sanctioned shell client, imported late so file/synthetic work without it."""
    import importlib
    import sys

    runtime = Path(__file__).resolve().parents[1] / "runtime"
    if str(runtime) not in sys.path:
        sys.path.insert(0, str(runtime))
    return importlib.import_module("camsh")


def _sh() -> Any:
    return _camsh().sh


@functools.lru_cache(maxsize=4)
def _helper_dir() -> Path:
    """Where the two camera-side helper binaries are expected to be built."""
    return Path(__file__).resolve().parents[1] / "builds" / "out"


def _serve_dir(directory: Path) -> tuple[socketserver.TCPServer, int]:
    """A one-shot HTTP server so the camera can `wget` the helpers.

    There is no `base64` on the device, so pushing a binary through the shell
    means octal `printf` per byte -- minutes, and fragile. Serving the directory
    and issuing one `wget` per file is a single round trip and the file arrives
    byte-exact. The server binds all interfaces because the camera has to reach
    it, and lives only for the length of the upload.
    """
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    httpd = socketserver.TCPServer(("0.0.0.0", 0), handler)  # noqa: S104
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, int(httpd.server_address[1])


def _local_ip_towards(host: str) -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return str(s.getsockname()[0])
    finally:
        s.close()


def ensure_helpers(host: str, names: tuple[str, ...], helper_dir: Path | None) -> None:
    """Upload the pull/dump helpers if they are not already resident.

    They are *not* baked into the firmware and are not expected to be: this is
    an investigation surface, so it is uploaded when wanted and removed after.
    Skipped entirely when the camera already has a byte-identical copy.
    """
    import hashlib

    sh = _sh()
    src = helper_dir or _helper_dir()
    missing = []
    for name in names:
        local = src / name
        if not local.is_file():
            raise LayerCaptureError(
                f"camera helper {name!r} is not built. Expected it at {local}. "
                f"Build it from {name}.c (see vpe/README) or use the file source."
            )
        want = hashlib.md5(local.read_bytes()).hexdigest()  # noqa: S324 -- integrity
        got = sh(f"md5sum {CAM_TMP}/{name} 2>/dev/null | cut -d' ' -f1", host=host)
        if want not in got:
            missing.append((name, want))
    if not missing:
        return
    httpd, port = _serve_dir(src)
    try:
        ip = _local_ip_towards(host)
        cmds = " && ".join(
            f"wget -q -O {CAM_TMP}/{n} http://{ip}:{port}/{n} && chmod 755 {CAM_TMP}/{n}"
            for n, _ in missing
        )
        sh(f"{cmds} && echo UPLOADED", host=host, timeout=60)
    finally:
        httpd.shutdown()
        httpd.server_close()
    for name, want in missing:
        got = sh(f"md5sum {CAM_TMP}/{name} | cut -d' ' -f1", host=host)
        if want not in got:
            raise LayerCaptureError(
                f"{name} did not arrive intact: wanted {want}, camera reports {got.strip()!r}"
            )


_PHYS_RE = re.compile(r"phys_Y\[168\]\s*=\s*(0x[0-9a-fA-F]+)")


def capture(
    host: str,
    *,
    helper_dir: Path | None = None,
    keep_helpers: bool = False,
) -> LayerPair:
    """Pull one pre-blend pair off the camera. Read-only, start to finish.

    THE ONE THING THAT MUST NOT BE SPLIT UP. The port's Y buffer address rotates
    over at least five buffers, and an address seen on an earlier pull gets
    *repurposed for another producer*. Dumping a remembered address therefore
    yields a foreign buffer that still looks like a plausible image -- one such
    read was a 2560-wide frame that only gave itself away under a stride scan.
    So the pull and the dump go out as ONE shell invocation, and the address
    never crosses back into Python before it has been used.

    The pull ioctl returns -14 whatever happens; the real result is `arg[0]`,
    which the helper reports. `pemdump` mmaps /dev/mem, so the physical offset
    must be 4 KB aligned -- an unaligned start silently produces no file, which
    is why the transfer is verified by size rather than by exit status.
    """
    camsh = _camsh()
    sh = camsh.sh
    ensure_helpers(host, ("isfpull2", "pemdump"), helper_dir)
    dump = f"{CAM_TMP}/seamlayers.bin"
    # Clearing the stale dump is its OWN invocation, and must stay that way.
    # `camsh.check()` refuses any command that contains both `rm` and a glob
    # metacharacter, scanning the whole string -- and the pull command below
    # contains `phys_Y[168]` in its sed expression. Combining them trips the
    # guard on a `[` that is nowhere near the `rm`. The guard is right to be
    # blunt about deleting things, so the command moves rather than the rule.
    sh(f"rm -f {dump}", host=host)
    one_shot = (
        f"A=$({CAM_TMP}/isfpull2 {PULL_TIMEOUT_MS} "
        f"0x{ISF_ROOT_PATH:08x} 0x{ISF_PORT_PATH:08x} "
        f"| sed -n 's/.*phys_Y\\[168\\][ ]*= \\(0x[0-9a-f]*\\).*/\\1/p' | sed -n 2p); "
        f"echo ADDR=$A; "
        f'[ -n "$A" ] && {CAM_TMP}/pemdump $A 0x{STRIP_Y_BYTES:x} {dump}; '
        f"echo SIZE=$(wc -c < {dump} 2>/dev/null || echo 0)"
    )
    out = sh(one_shot, host=host, timeout=60)
    addr = ""
    size = 0
    for line in out.splitlines():
        if line.startswith("ADDR="):
            addr = line[5:].strip()
        elif line.startswith("SIZE="):
            try:
                size = int(line[5:].strip())
            except ValueError:
                size = 0
    if not addr:
        raise LayerCaptureError(
            "the pull returned no buffer address for out[1]. The port may not be "
            f"producing. Helper said:\n{out.strip()}"
        )
    if size != STRIP_Y_BYTES:
        raise LayerCaptureError(
            f"dump of {addr} is {size} B, expected {STRIP_Y_BYTES} B -- "
            "an unaligned or refused /dev/mem read produces a short or absent file"
        )
    # `cat` over the probe shell is binary-safe and needs no base64 on either end.
    blob = camsh.sh_bytes(f"cat {dump}", host=host, timeout=120)
    if len(blob) < STRIP_Y_BYTES:
        raise LayerCaptureError(
            f"retrieved {len(blob)} B of {STRIP_Y_BYTES} B from {dump}"
        )
    if not keep_helpers:
        # Exact paths only. Globs inside /mnt/tmp are how a cleanup step once
        # deleted 236 GB of recordings; camsh refuses them and so does this.
        sh(
            f"rm -f {dump} {CAM_TMP}/isfpull2 {CAM_TMP}/pemdump; echo CLEANED",
            host=host,
        )
    return split_packed(
        blob,
        source="camera",
        detail=f"live pull from {host}, out[1] buffer {addr}",
    )


#: Sources that produce an ALIGNMENT-CAPABLE pair -- one in panorama output
#: coordinates, where a measured displacement converts straight to dx_anchors.
SOURCES = ("camera", "file", "synthetic")


# --------------------------------------------------------------------------
# Whole-lens context views -- deliberately NOT a LayerPair
# --------------------------------------------------------------------------


#: The vendor's own per-sensor snap. Message 13010, not 13003: 13003 hardcodes a
#: task class that can only ever build `yuv_snap`. The parameter must be exactly
#: 32 hex characters or the call returns -803. Success is silent; failure prints
#: `to:SNAP error,ret :-822`.
RPC_SNAP_MSG = 13010
RPC_SNAP_PARAM = "02" + "0" * 30
RPC_SNAP_SETTLE_S = 5


@dataclass
class SensorViews:
    """Two whole-lens JPEGs. Context for the operator, never a measurement.

    WHY THIS IS A DIFFERENT TYPE FROM `LayerPair`, AND MUST STAY ONE.

    These frames are 3840x2160 -- the native width of each sensor's own output
    port -- which means they are in **sensor coordinates, before the warp**. A
    displacement measured on them is not a panorama displacement and does not
    convert to `dx_anchors` without the sensor->panorama warp.

    That warp has since been measured (a 2-D cubic fitted by matching panorama
    patches into each sensor frame outside the blend band; it reproduces the
    panorama to ~2-5 grey levels, and each sensor carries 450-530 columns of
    genuine margin past the seam). So this is a scope boundary, not a claim of
    impossibility. It is not wired in here for three reasons: the strip pair is
    pre-rectified by the hardware and so carries no fit error at all; the fitted
    maps are archived artifacts a camera-manager install does not have; and the
    fit is tied to the current mesh, which is the very thing a calibration
    changes. Resampling a sensor frame through that map would produce an
    ordinary `LayerPair` -- see the class docstring there.

    A `LayerPair` is the opposite: already resampled into the panorama's output
    frame, so a displacement on it *is* the correction. Both could be described
    as "two images of the same scene from the two sensors", and that shared
    description is exactly the trap. Giving them one type would make it a
    reviewer's job to remember which instance was authoritative; giving them
    two makes it the type checker's. The endpoint that serves these cannot
    return a LayerPair and the UI cannot bind a gesture to them.

    (An earlier reading had these at 3904 wide and argued the extra 64 columns
    per side were the overlap margin, making them alignment-capable. Three
    fresh captures read 3840 in their SOF0 headers, so that does not reproduce
    and the argument is withdrawn.)
    """

    left: bytes  # JPEG, sensor 0
    right: bytes  # JPEG, sensor 1
    width: int
    height: int
    stamp: str
    source: str = "camera"
    #: Named to be read at the call site. There is no code path that sets this
    #: to "panorama" -- that is what `LayerPair` is for.
    space: str = "sensor"

    def to_api(self) -> dict:
        return {
            "source": self.source,
            "space": self.space,
            "authoritative": False,
            "width": int(self.width),
            "height": int(self.height),
            "stamp": self.stamp,
            "why": (
                "whole-lens views in sensor coordinates, before the warp. Use "
                "them to see what each lens is looking at; the alignment "
                "surface is the pre-rectified overlap pair."
            ),
        }


def _jpeg_size(blob: bytes) -> tuple[int, int]:
    """Width and height from the SOF marker. Trust the file, not the caller."""
    import struct

    i = 2
    while i < len(blob) - 9:
        if blob[i] != 0xFF:
            i += 1
            continue
        marker = blob[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", blob[i + 5 : i + 9])
            return int(w), int(h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", blob[i + 2 : i + 4])[0]
    raise LayerCaptureError("no JPEG SOF marker -- that is not a JPEG")


def capture_sensor_views(host: str, *, keep: bool = False) -> SensorViews:
    """Ask the camera for one matched per-sensor still. Read-only.

    Needs no upload, no build and no login: `rpctool` is already on the device.
    The two files share a timestamp when they are a matched pair, which is the
    only thing that makes them comparable, so a mismatched pair is refused
    rather than shown.

    `/mnt/tmp` is a 55 MB tmpfs, so the files are removed by exact path after
    retrieval -- never by glob.
    """
    camsh = _camsh()
    sh = camsh.sh
    cmd = (
        "export LD_LIBRARY_PATH=/lib:/usr/lib:/usr/local/lib:/mnt/app; "
        f"/mnt/app/rpctool -s -t SNAP -c {RPC_SNAP_MSG} -m {RPC_SNAP_PARAM} 2>&1; "
        f"sleep {RPC_SNAP_SETTLE_S}; "
        "ls -1 /mnt/tmp/01_*.jpg /mnt/tmp/02_*.jpg 2>/dev/null"
    )
    out = sh(cmd, host=host, timeout=90)
    if "error" in out.lower():
        raise LayerCaptureError(f"the per-sensor snap refused:\n{out.strip()}")
    found: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"^(/mnt/tmp/(0[12])_(\d+)\.jpg)$", line)
        if m:
            found.setdefault(m.group(3), {})[m.group(2)] = m.group(1)
    matched = sorted(k for k, v in found.items() if "01" in v and "02" in v)
    if not matched:
        raise LayerCaptureError(
            "the snap produced no matched 01_/02_ pair in /mnt/tmp. "
            f"Shell said:\n{out.strip()}"
        )
    stamp = matched[-1]
    paths = found[stamp]
    blobs = {}
    for side, key in (("left", "01"), ("right", "02")):
        blob = camsh.sh_bytes(f"cat {paths[key]}", host=host, timeout=120)
        if len(blob) < 1024:
            raise LayerCaptureError(f"{paths[key]} came back as {len(blob)} B")
        blobs[side] = blob
    w, h = _jpeg_size(blobs["left"])
    w2, h2 = _jpeg_size(blobs["right"])
    if (w, h) != (w2, h2):
        raise LayerCaptureError(
            f"the two sensor views disagree on size: {w}x{h} vs {w2}x{h2}"
        )
    if not keep:
        sh(f"rm -f {paths['01']} {paths['02']}; echo CLEANED", host=host)
    return SensorViews(
        left=blobs["left"], right=blobs["right"], width=w, height=h, stamp=stamp
    )


def load_sensor_views(left: str | Path, right: str | Path) -> SensorViews:
    """Archived per-sensor stills, for working without a camera on the link."""
    lp, rp = Path(left), Path(right)
    for p in (lp, rp):
        if not p.is_file():
            raise LayerCaptureError(f"no such sensor view: {p}")
    lb, rb = lp.read_bytes(), rp.read_bytes()
    w, h = _jpeg_size(lb)
    return SensorViews(
        left=lb, right=rb, width=w, height=h, stamp=lp.stem, source="file"
    )
