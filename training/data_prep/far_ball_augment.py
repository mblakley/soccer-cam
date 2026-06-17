"""Far-ball cut-paste augmentation for the heatmap detector (R7).

**Key finding (EXP-23, 2026-06-17).** The real far ball, on the warped band, is a small
**low-contrast, often dark blob** — NOT a bright white sphere. (Validated by cropping the
human-labelled far balls and comparing to rendered spheres: the spheres looked nothing
like the real thing.) This *explains* why the ball ranks 4–12 among detector candidates
(it's genuinely low-contrast, so brightness-based scoring ranks it below bright lines /
kit / tents). So fully-synthetic white-sphere balls are the wrong training signal.

Instead, **cut-paste real labelled ball patches** onto new field locations and
backgrounds, with scale / contrast / flip jitter (domain randomisation). This multiplies
the scarce far-ball examples while preserving the realistic low-contrast appearance the
detector must learn — the lever that makes the ball discriminable (EXP-22: selection is
maxed; the detector making the ball not-dim is the only remaining accuracy lever).

Pure numpy + cv2. Operates on the grayscale warped band (the detector's input space).
"""

from __future__ import annotations

import cv2
import numpy as np


def crop_ball_patch(
    band: np.ndarray, bx: float, by: float, r: int = 14
) -> tuple[np.ndarray, np.ndarray] | None:
    """Crop a feathered ball patch centred on band pixel ``(bx, by)``.

    Returns ``(patch, alpha)`` — a ``(2r, 2r)`` grayscale patch and a radial alpha
    mask (solid in the centre, feathered to 0 by ~0.6·r so the paste blends into a
    new background without a hard square edge). Returns ``None`` if the crop would
    fall outside the band.
    """
    bx, by = int(round(bx)), int(round(by))
    h, w = band.shape[:2]
    if bx - r < 0 or by - r < 0 or bx + r > w or by + r > h:
        return None
    patch = band[by - r : by + r, bx - r : bx + r].astype(np.float32)
    yy, xx = np.mgrid[0 : 2 * r, 0 : 2 * r].astype(np.float32)
    dist = np.hypot(xx - r, yy - r)
    alpha = np.clip((r * 0.6 - dist) / (r * 0.35), 0.0, 1.0).astype(np.float32)
    return patch, alpha


def paste_ball(
    bg: np.ndarray,
    patch: np.ndarray,
    alpha: np.ndarray,
    bx: float,
    by: float,
    scale: float = 1.0,
    contrast: float = 1.0,
    flip: bool = False,
) -> bool:
    """Alpha-composite a (jittered) ball patch onto ``bg`` (modified in place).

    Args:
        bg: grayscale band background (uint8), modified in place.
        patch, alpha: from :func:`crop_ball_patch`.
        bx, by: paste centre in band pixels.
        scale: resize factor (size jitter — the band roughly perspective-normalises
            ball size, so keep this near 1, e.g. 0.8–1.4).
        contrast: local contrast jitter about the patch mean (lighting variation —
            sunny vs shadowed ball).
        flip: horizontal flip.

    Returns:
        ``True`` if pasted; ``False`` if it would fall outside ``bg``.
    """
    p = patch.copy()
    a = alpha.copy()
    if flip:
        p = p[:, ::-1].copy()
        a = a[:, ::-1].copy()
    s = max(3, int(round(p.shape[0] * scale)) | 1)
    p = cv2.resize(p, (s, s))
    a = cv2.resize(a, (s, s))
    if contrast != 1.0:
        m = float(p.mean())
        p = np.clip((p - m) * contrast + m, 0, 255)
    y0, x0 = int(round(by - s // 2)), int(round(bx - s // 2))
    h, w = bg.shape[:2]
    if y0 < 0 or x0 < 0 or y0 + s > h or x0 + s > w:
        return False
    roi = bg[y0 : y0 + s, x0 : x0 + s].astype(np.float32)
    bg[y0 : y0 + s, x0 : x0 + s] = (a * p + (1.0 - a) * roi).astype(np.uint8)
    return True


def sample_field_locations(
    field_mask: np.ndarray, n: int, rng: np.random.Generator
) -> list[tuple[int, int]]:
    """Sample ``n`` random ``(bx, by)`` band locations inside ``field_mask`` (>0)."""
    ys, xs = np.where(field_mask > 0)
    if len(xs) == 0:
        return []
    idx = rng.integers(0, len(xs), size=n)
    return [(int(xs[i]), int(ys[i])) for i in idx]
