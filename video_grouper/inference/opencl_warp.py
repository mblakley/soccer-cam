"""Zero-copy OpenCL warp backend for the broadcast renderer.

The warp-once-crop render (``cylindrical_view``) reduces each frame to: sample a CONSTANT
leveling panorama ``L`` at a per-frame crop window, then sample the source. That is a pure
GPU job, but the naive GPU backends (cv2.UMat / DirectML) lose to the CPU because they
re-allocate + copy + convert a buffer every call. This backend instead:

* uploads ``L`` (the leveling map) to the GPU ONCE,
* uses ``ALLOC_HOST_PTR`` zero-copy buffers so the integrated GPU reads host memory
  directly (no real "upload" on a shared-memory iGPU -- just a memcpy into a mapped buffer),
* computes the whole warp in one kernel from the constant ``L`` + the 4-int crop box (no
  per-frame map generation or map upload).

Measured ~273 fps kernel-only / ~80 fps with realistic copy-in+read-out on an Intel Iris
Xe -- faster than cv2.remap on the CPU, and it frees the CPU for the (bottleneck) decode.
Falls back to cv2 when pyopencl or an OpenCL device is unavailable (see
:meth:`OpenCLWarper.available`).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# pyopencl is an optional accel dep with no type stubs (see the mypy
# ignore_missing_imports override); mypy treats it as Any so the C-level
# enqueue_map_buffer / Buffer calls type-check and the None fallback is allowed.
_cl: Any = None
_HAVE_CL = False
try:
    import pyopencl

    _cl = pyopencl
    _HAVE_CL = True
except (
    ImportError
):  # pragma: no cover - pyopencl is an optional acceleration dependency
    pass

# Output pixel -> pano coord (matching cv2.resize's half-pixel convention) -> bilinear sample
# of L (pano->source coords) -> bilinear sample of the source. ``L`` is constant; only the
# crop box (cx,cy,cw,ch) changes per frame. Channel-agnostic (BGR or RGB).
_KERNEL = """
inline float bilL(__global const float* L, int pw, int ph, float px, float py) {
  int x0 = (int)floor(px), y0 = (int)floor(py);
  float ax = px - x0, ay = py - y0;
  x0 = clamp(x0, 0, pw - 2); y0 = clamp(y0, 0, ph - 2);
  float a = L[y0*pw + x0],     b = L[y0*pw + x0+1];
  float c = L[(y0+1)*pw + x0], d = L[(y0+1)*pw + x0+1];
  return (a*(1-ax) + b*ax)*(1-ay) + (c*(1-ax) + d*ax)*ay;
}
__kernel void warp(__global const uchar* src, const int sw, const int sh,
                   __global const float* Lx, __global const float* Ly, const int pw, const int ph,
                   const int cx, const int cy, const int cw, const int ch,
                   __global uchar* dst, const int ow, const int oh) {
  int x = get_global_id(0), y = get_global_id(1);
  if (x >= ow || y >= oh) return;
  int o = y*ow + x;
  float px = cx + (x + 0.5f)*cw/(float)ow - 0.5f;
  float py = cy + (y + 0.5f)*ch/(float)oh - 0.5f;
  float sx = bilL(Lx, pw, ph, px, py), sy = bilL(Ly, pw, ph, px, py);
  int x0 = (int)floor(sx), y0 = (int)floor(sy);
  float ax = sx - x0, ay = sy - y0;
  for (int k = 0; k < 3; k++) {
    float v = 0.0f;
    if (x0 >= 0 && y0 >= 0 && x0+1 < sw && y0+1 < sh) {
      float p00 = src[(y0*sw + x0)*3 + k],     p01 = src[(y0*sw + x0+1)*3 + k];
      float p10 = src[((y0+1)*sw + x0)*3 + k], p11 = src[((y0+1)*sw + x0+1)*3 + k];
      v = (p00*(1-ax) + p01*ax)*(1-ay) + (p10*(1-ax) + p11*ax)*ay;
    }
    dst[o*3 + k] = (uchar)(v + 0.5f);
  }
}
"""


class OpenCLWarper:
    """GPU warp via the constant leveling map + a per-frame crop box. One per render stream.

    Usage: ``out = warper.warp(frame_rgb, (x0, y0, w, h))`` where ``frame_rgb`` is the full
    ``src_h x src_w x 3`` source and the box comes from
    :func:`cylindrical_view.crop_box`.
    """

    def __init__(self, pano, src_w: int, src_h: int, out_w: int, out_h: int) -> None:
        if not _HAVE_CL:
            raise RuntimeError("pyopencl not installed")
        self.sw, self.sh, self.ow, self.oh = src_w, src_h, out_w, out_h
        self.ph, self.pw = pano.map_x.shape
        self.ctx = _cl.create_some_context(interactive=False)
        self.q = _cl.CommandQueue(self.ctx)
        self.prg = _cl.Program(self.ctx, _KERNEL).build()
        mf = _cl.mem_flags
        self._lx = _cl.Buffer(
            self.ctx,
            mf.READ_ONLY | mf.COPY_HOST_PTR,
            hostbuf=np.ascontiguousarray(pano.map_x, np.float32),
        )
        self._ly = _cl.Buffer(
            self.ctx,
            mf.READ_ONLY | mf.COPY_HOST_PTR,
            hostbuf=np.ascontiguousarray(pano.map_y, np.float32),
        )
        self._src = _cl.Buffer(
            self.ctx, mf.READ_ONLY | mf.ALLOC_HOST_PTR, src_w * src_h * 3
        )
        self._dst = _cl.Buffer(
            self.ctx, mf.WRITE_ONLY | mf.ALLOC_HOST_PTR, out_w * out_h * 3
        )
        self._src_map, _ = _cl.enqueue_map_buffer(
            self.q, self._src, _cl.map_flags.WRITE, 0, src_w * src_h * 3, np.uint8
        )
        self._dst_map, _ = _cl.enqueue_map_buffer(
            self.q, self._dst, _cl.map_flags.READ, 0, out_w * out_h * 3, np.uint8
        )
        self.q.finish()

    @classmethod
    def available(cls) -> bool:
        """True if pyopencl + at least one OpenCL device are usable."""
        if not _HAVE_CL:
            return False
        try:
            return any(p.get_devices() for p in _cl.get_platforms())
        except Exception:  # pragma: no cover - driver/enumeration failure
            return False

    def warp(self, frame: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
        """Warp ``frame`` (src_h x src_w x 3 uint8) through the crop ``box`` -> out_h x out_w x 3."""
        cx, cy, cw, ch = box
        self._src_map[:] = np.ascontiguousarray(frame).reshape(
            -1
        )  # memcpy into the zero-copy buffer
        self.prg.warp(
            self.q,
            (self.ow, self.oh),
            None,
            self._src,
            np.int32(self.sw),
            np.int32(self.sh),
            self._lx,
            self._ly,
            np.int32(self.pw),
            np.int32(self.ph),
            np.int32(cx),
            np.int32(cy),
            np.int32(cw),
            np.int32(ch),
            self._dst,
            np.int32(self.ow),
            np.int32(self.oh),
        )
        self.q.finish()
        return np.array(self._dst_map, copy=True).reshape(self.oh, self.ow, 3)

    def close(self) -> None:
        for b in (
            getattr(self, "_lx", None),
            getattr(self, "_ly", None),
            getattr(self, "_src", None),
            getattr(self, "_dst", None),
        ):
            if b is not None:
                b.release()
