"""Keypoint-aware augmentation for field-boundary distillation.

With only ~20 distinct camera placements, augmentation — not model
capacity — is what makes the student generalize. Everything operates on
the stored ~1920px RGB frame and transforms the 10 keypoints in lockstep,
then squashes to the teacher's 768x384 input geometry.

Coordinates are normalized ``[0, 1]`` throughout. The 10 points are
ordered: 0-4 near sideline left->right, 5-9 far boundary right->left.

Pure helpers (``flip_keypoints``, ``transform_keypoints_for_crop``) are
unit-tested; the index remap on horizontal flip is correctness-critical.
"""

from __future__ import annotations

import cv2
import numpy as np

from training.field_outline import INPUT_H, INPUT_W, NUM_KEYPOINTS

# Horizontal-flip index remap: keeps semantics (index 0 stays "near-left")
# after mirroring. near i -> 4-i, far i -> 14-i.
FLIP_REMAP = np.array([4, 3, 2, 1, 0, 9, 8, 7, 6, 5], dtype=np.int64)

# A point with teacher score below this is not used for coordinate loss.
COORD_SCORE_MIN = 0.5

# Crop sampling ranges.
_CROP_WIDTH_FRAC = (0.70, 1.0)
_CROP_ASPECT = (1.8, 4.0)  # deployment squash spans ~2.28:1 .. 3.56:1
_MAX_POINTS_LOST = 3
_CROP_TRIES = 8


def flip_keypoints(
    kpts: np.ndarray, scores: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Mirror keypoints + scores for a horizontal image flip.

    ``kpts`` is ``(10, 2)`` normalized, ``scores`` is ``(10,)``. Reorders
    by :data:`FLIP_REMAP` and mirrors x (``x -> 1 - x``) so index meaning
    is preserved. Involution: applying twice is the identity.
    """
    out_k = kpts[FLIP_REMAP].copy()
    out_k[:, 0] = 1.0 - out_k[:, 0]
    out_s = scores[FLIP_REMAP].copy()
    return out_k, out_s


def random_crop_box(
    w: int, h: int, rng: np.random.Generator
) -> tuple[int, int, int, int]:
    """Sample a crop ``(x0, y0, cw, ch)`` in pixels with aspect jitter."""
    cw = int(rng.uniform(*_CROP_WIDTH_FRAC) * w)
    aspect = rng.uniform(*_CROP_ASPECT)
    ch = min(int(cw / aspect), h)
    cw = min(cw, w)
    x0 = int(rng.integers(0, w - cw + 1))
    y0 = int(rng.integers(0, h - ch + 1))
    return x0, y0, cw, ch


def transform_keypoints_for_crop(
    kpts: np.ndarray, w: int, h: int, box: tuple[int, int, int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Map normalized keypoints into a crop box's normalized frame.

    Returns ``(new_kpts, in_frame)``. ``new_kpts`` is clamped to ``[0, 1]``;
    ``in_frame`` marks points that actually fell inside the crop (the others
    must not be supervised for coordinates).
    """
    x0, y0, cw, ch = box
    px = kpts[:, 0] * w
    py = kpts[:, 1] * h
    nx = (px - x0) / cw
    ny = (py - y0) / ch
    in_frame = (nx >= 0) & (nx <= 1) & (ny >= 0) & (ny <= 1)
    new = np.stack([np.clip(nx, 0.0, 1.0), np.clip(ny, 0.0, 1.0)], axis=1)
    return new.astype(np.float32), in_frame


def _photometric(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Brightness/contrast/hue/sat/gamma/noise/blur/jpeg on uint8 RGB."""
    out = img
    # brightness/contrast
    alpha = float(rng.uniform(0.7, 1.3))  # contrast
    beta = float(rng.uniform(-30, 30))  # brightness
    out = cv2.convertScaleAbs(out, alpha=alpha, beta=beta)
    # hue/saturation
    hsv = cv2.cvtColor(out, cv2.COLOR_RGB2HSV).astype(np.int16)
    hsv[..., 0] = (hsv[..., 0] + int(rng.uniform(-10, 10))) % 180
    hsv[..., 1] = np.clip(hsv[..., 1] * rng.uniform(0.7, 1.3), 0, 255)
    out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
    # gamma
    gamma = float(rng.uniform(0.75, 1.4))
    lut = np.clip(((np.arange(256) / 255.0) ** (1.0 / gamma)) * 255, 0, 255)
    out = cv2.LUT(out, lut.astype(np.uint8))
    # gaussian noise
    if rng.random() < 0.5:
        noise = rng.normal(0, rng.uniform(2, 10), out.shape).astype(np.float32)
        out = np.clip(out.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    # occasional blur
    if rng.random() < 0.2:
        k = int(rng.choice([3, 5]))
        out = cv2.GaussianBlur(out, (k, k), 0)
    # occasional jpeg recompression
    if rng.random() < 0.3:
        q = int(rng.integers(40, 85))
        ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, q])
        if ok:
            out = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    return out


def _random_occlusion(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Drop 1-4 rectangles (<=8% area each); keypoint targets are kept."""
    out = img.copy()
    h, w = out.shape[:2]
    for _ in range(int(rng.integers(1, 5))):
        rw = int(rng.uniform(0.05, 0.28) * w)
        rh = int((0.08 * w * h) / max(rw, 1))
        rh = min(rh, h)
        x0 = int(rng.integers(0, max(w - rw, 1)))
        y0 = int(rng.integers(0, max(h - rh, 1)))
        if rng.random() < 0.5:
            patch = rng.integers(0, 256, (rh, rw, 3), dtype=np.uint8)
        else:
            patch = np.full((rh, rw, 3), out.mean(axis=(0, 1)), dtype=np.uint8)
        out[y0 : y0 + rh, x0 : x0 + rw] = patch
    return out


def augment_sample(
    img_rgb: np.ndarray,
    kpts: np.ndarray,
    scores: np.ndarray,
    rng: np.random.Generator,
    train: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Augment one sample to the 768x384 training input.

    Returns ``(image, kpts, scores, in_frame)``:
      - ``image``: ``float32`` RGB ``[0, 1]`` of shape ``(384, 768, 3)``
      - ``kpts``: ``(10, 2)`` normalized, clamped to ``[0, 1]``
      - ``scores``: ``(10,)`` teacher scores, reordered on flip and zeroed
        for points pushed out of frame
      - ``in_frame``: ``(10,)`` bool — points eligible for coordinate loss

    Validation (``train=False``) is the deployment path: plain squash to
    768x384, no geometric or photometric changes.
    """
    kpts = np.asarray(kpts, dtype=np.float32).reshape(NUM_KEYPOINTS, 2)
    scores = np.asarray(scores, dtype=np.float32).reshape(NUM_KEYPOINTS)

    if not train:
        img = cv2.resize(img_rgb, (INPUT_W, INPUT_H)).astype(np.float32) / 255.0
        return img, kpts.copy(), scores.copy(), np.ones(NUM_KEYPOINTS, bool)

    img = img_rgb
    # 1. horizontal flip
    if rng.random() < 0.5:
        img = img[:, ::-1]
        kpts, scores = flip_keypoints(kpts, scores)

    # 2. crop + aspect jitter (resample until <=3 points are lost)
    h, w = img.shape[:2]
    box = (0, 0, w, h)
    new_kpts, in_frame = kpts, np.ones(NUM_KEYPOINTS, bool)
    for _ in range(_CROP_TRIES):
        cand = random_crop_box(w, h, rng)
        ck, cif = transform_keypoints_for_crop(kpts, w, h, cand)
        if (~cif).sum() <= _MAX_POINTS_LOST:
            box, new_kpts, in_frame = cand, ck, cif
            break
    x0, y0, cw, ch = box
    img = np.ascontiguousarray(img[y0 : y0 + ch, x0 : x0 + cw])
    kpts = new_kpts
    scores = np.where(in_frame, scores, 0.0).astype(np.float32)

    # 3. resize to training geometry, 4. photometric, 5. occlusion
    img = cv2.resize(img, (INPUT_W, INPUT_H))
    img = _photometric(img, rng)
    img = _random_occlusion(img, rng)

    img = img.astype(np.float32) / 255.0
    return img, kpts, scores, in_frame
