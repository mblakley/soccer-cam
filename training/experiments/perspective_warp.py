"""Perspective-normalization validation experiment.

Measures TRUE ball pixel diameter vs field row from high-confidence detections
(the detector's own bbox sizes are unreliable — it emits ~fixed-size boxes),
reports the far->near apparent-size gradient, builds a vertical scale-normalizing
warp (compress near rows, preserve far), quantifies the far-ball-pixels vs
input-width trade-off, and renders sample raw+warped frames for inspection.

Usage: perspective_warp.py <video> <detections.json> <out_dir>
Run on the server; video staged on G: for speed. cv2 + PyAV only (no GPU).
"""

import json
import os
import sys

import av
import cv2
import numpy as np

VIDEO, DETS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)

det = json.load(open(DETS))
hi = sorted((d for d in det if d["conf"] >= 0.55), key=lambda d: d["frame_idx"])
print(f"high-conf detections (>=0.55): {len(hi)}", flush=True)


def grab(c, s, idx):
    off = int(idx / float(s.average_rate) / s.time_base)
    c.seek(off, stream=s, backward=True)
    for fr in c.decode(s):
        fi = int(round(float(fr.pts * s.time_base) * float(s.average_rate)))
        if fi >= idx:
            return fr.to_ndarray(format="bgr24")
    return None


def measure_ball(frame, cx, cy, win=120):
    """True ball diameter via the central bright blob; None if not a clean ball."""
    H, W = frame.shape[:2]
    x0 = int(np.clip(cx - win // 2, 0, W - win))
    y0 = int(np.clip(cy - win // 2, 0, H - win))
    g = cv2.cvtColor(frame[y0 : y0 + win, x0 : x0 + win], cv2.COLOR_BGR2GRAY)
    cxl, cyl = int(cx) - x0, int(cy) - y0
    if not (5 <= cxl < win - 5 and 5 <= cyl < win - 5):
        return None
    cb = float(g[cyl - 3 : cyl + 4, cxl - 3 : cxl + 4].mean())
    bg = float(np.median(g))
    if cb - bg < 22:  # no clear bright ball vs background
        return None
    mask = (g >= (cb + bg) / 2).astype(np.uint8)
    n, _, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    best, bd = None, 1e9
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < 7:
            continue
        d = (cent[i][0] - cxl) ** 2 + (cent[i][1] - cyl) ** 2
        if d < bd:
            bd, best = d, i
    if best is None or bd > 350:
        return None
    w_, h_ = stats[best, cv2.CC_STAT_WIDTH], stats[best, cv2.CC_STAT_HEIGHT]
    if max(w_, h_) > 60:  # line / jersey, not a ball
        return None
    return (w_ + h_) / 2.0


# --- measure across a spread sample ---
sample = hi[:: max(1, len(hi) // 800)]
c = av.open(VIDEO)
s = c.streams.video[0]
s.thread_type = "AUTO"
H, Wd = s.height, s.width
rows, sizes = [], []
for k, d in enumerate(sample):
    fr = grab(c, s, d["frame_idx"])
    if fr is not None:
        sz = measure_ball(fr, d["cx"], d["cy"])
        if sz:
            rows.append(d["cy"])
            sizes.append(sz)
    if k % 150 == 0:
        print(f"  {k}/{len(sample)} measured={len(sizes)}", flush=True)
c.close()
rows, sizes = np.array(rows), np.array(sizes)
print(f"\nframe {Wd}x{H}; measured {len(sizes)} true balls", flush=True)

# --- gradient by row band ---
print("\n=== TRUE ball diameter vs field row ===", flush=True)
bands = np.linspace(rows.min(), rows.max(), 9)
ys, szs = [], []
for i in range(len(bands) - 1):
    m = (rows >= bands[i]) & (rows < bands[i + 1])
    if m.sum() >= 8:
        md = float(np.median(sizes[m]))
        ys.append((bands[i] + bands[i + 1]) / 2)
        szs.append(md)
        print(
            f"  row {bands[i]:5.0f}-{bands[i + 1]:5.0f}: n={m.sum():3d} median_diam={md:5.1f}px",
            flush=True,
        )
ys, szs = np.array(ys), np.array(szs)
far = float(np.median(sizes[rows < np.percentile(rows, 25)]))
near = float(np.median(sizes[rows > np.percentile(rows, 75)]))
print(
    f"FAR(top 25%) {far:.1f}px ; NEAR(bottom 25%) {near:.1f}px ; ratio {near / far:.2f}x",
    flush=True,
)

# --- vertical scale-normalizing warp (target = far size -> far kept native) ---
y_top, y_bot = int(ys.min()), int(ys.max())
target = far
src = np.arange(y_top, y_bot + 1)
sc = np.array([min(1.0, target / float(np.interp(y, ys, szs))) for y in src])
out_y = np.concatenate([[0.0], np.cumsum(sc)])[:-1]
out_h = int(out_y[-1]) + 1
print(
    f"\nvertical warp: field rows {y_top}-{y_bot} ({y_bot - y_top}px) -> {out_h}px "
    f"(compression {(y_bot - y_top) / out_h:.2f}x)",
    flush=True,
)

# --- far-ball pixels vs input width trade-off (vs AutoCam-style 3264) ---
print(
    "\n=== far-ball pixels vs input width (single full-frame, no tiles) ===", flush=True
)
tiles_px = 7 * 3 * 640 * 640
for tw in (3264, 3840, 4096, 5120):
    sx = tw / Wd
    far_px = far * sx  # far ball size after horizontal downscale to tw
    total = tw * int(out_h * sx)
    print(
        f"  width {tw}: far ball ~{far_px:4.1f}px, input {tw}x{int(out_h * sx)} "
        f"= {total / 1e6:4.1f}MP  (vs 21-tile {tiles_px / 1e6:.1f}MP, {total / tiles_px * 100:.0f}%)",
        flush=True,
    )

# --- render sample raw + warped frames ---
inv = np.interp(np.arange(out_h), out_y, src)
map_y = (inv - y_top).astype(np.float32)[:, None].repeat(Wd, axis=1)
map_x = np.arange(Wd, dtype=np.float32)[None, :].repeat(out_h, axis=0)
TW = 3840
c = av.open(VIDEO)
s = c.streams.video[0]
s.thread_type = "AUTO"
for fidx in [d["frame_idx"] for d in sample[:: max(1, len(sample) // 3)]][:3]:
    fr = grab(c, s, fidx)
    if fr is None:
        continue
    warped = cv2.remap(fr[y_top : y_bot + 1], map_x, map_y, cv2.INTER_AREA)
    ws = cv2.resize(warped, (TW, int(out_h * TW / Wd)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(os.path.join(OUT, f"warp_{fidx:06d}.png"), ws)
    cv2.imwrite(
        os.path.join(OUT, f"raw_{fidx:06d}.png"),
        cv2.resize(fr, (TW, int(H * TW / Wd))),
    )
c.close()
print("\nWARP-DONE; wrote sample raw+warped frames to", OUT, flush=True)
