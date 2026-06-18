"""Far-ball cut-paste augmentation for the heatmap detector (R7), physics/game-aware.

**Key finding (EXP-23, 2026-06-17).** The real far ball, on the warped band, is a small
**low-contrast, often dark blob** — NOT a bright white sphere. (Validated by cropping the
human-labelled far balls and comparing to rendered spheres: the spheres looked nothing
like the real thing.) This *explains* why the ball ranks 4-12 among detector candidates
(it's genuinely low-contrast, so brightness-based scoring ranks it below bright lines /
kit / tents). So fully-synthetic white-sphere balls are the wrong training signal.

Instead, **cut-paste real labelled ball patches** onto new field locations and
backgrounds, with scale / contrast / flip jitter (domain randomisation). This multiplies
the scarce far-ball examples while preserving the realistic low-contrast appearance the
detector must learn.

**The augmentation must obey the same world-model rules as the ball itself (EXP-25/26).**
A first pass scattered the ball at a uniform-random pixel with iid per-frame jitter; a
second added a *random* coherent velocity plus synthetic motion blur along it. Both broke
**camera physics: the ball's velocity determines what it looks like.** Measurement (EXP-26)
showed **54% of the real cut-paste source balls are already motion-blurred** (blob
elongation > 1.5, up to 12x) — a real moving ball is smeared into a streak along *its*
velocity. Bolting a *random* velocity (and an extra synthetic blur, and a random flip that
reverses the streak) onto such a patch trains the detector on impossible balls: streaked
one way, moving another. The detector reads the 3-frame stack precisely to tell a real
*moving* ball from static bright distractors, so the paste must be physically self-consistent:

- **Velocity FROM appearance (camera-physics rule).** Read the ball's velocity from its own
  motion blur — streak *orientation* = direction, streak *excess length* over the disc width
  = per-frame speed (:func:`estimate_ball_velocity`) — then translate the **real** patch
  (real blur intact) along *that* axis at *that* speed. No synthetic blur, no contradiction.
  A round/sharp source is a slow ball: near-static placement, direction-agnostic.
- **Temporal continuity (no teleport).** The ball traces that straight path across the 3
  frames. The stack is ``[t-2, t-1, t]`` and the target is frame ``t``, so the ball in the
  LAST frame sits exactly on the label; earlier frames are offset *backwards* along velocity.
- **Field support (on-field only).** The ball is pasted only where the whole motion path's
  footprint lands inside the field mask (off-field is hard-zeroed in the crop) — no frame
  off-field, no ball-on-black.
- **Flip mirrors velocity.** A horizontal flip mirrors the streak, so it must mirror the
  velocity too, or appearance and motion decouple again.

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
            ball size, so keep this near 1, e.g. 0.8-1.4).
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


def sample_velocity(
    rng: np.random.Generator, max_speed: float = 22.0, slow_scale: float = 6.0
) -> tuple[float, float]:
    """Sample a coherent per-frame ball velocity ``(vx, vy)`` in band px/frame.

    Models the real far-ball speed distribution: mostly slow (~3-4 px/frame median,
    EXP overnight) with a fast tail (kicks), hard-capped at ``max_speed`` (air drag /
    the no-teleport max-speed rule). Speed is a clipped half-normal; direction uniform.
    """
    speed = float(np.clip(abs(rng.normal(0.0, slow_scale)), 0.0, max_speed))
    ang = float(rng.uniform(0.0, 2.0 * np.pi))
    return (speed * np.cos(ang), speed * np.sin(ang))


def onfield_mask(stack: np.ndarray) -> np.ndarray:
    """On-field boolean mask from a masked crop.

    ``heatmap_dataset`` hard-zeroes every off-field pixel, so on-field == strictly
    positive in every frame. Used to keep the paste (and its whole motion path) on the
    pitch — the field-support game rule.
    """
    return stack.min(axis=0) > 0


def sample_onfield_location(
    mask: np.ndarray, r: int, rng: np.random.Generator
) -> tuple[float, float] | None:
    """Sample ``(cx, cy)`` where a ball of footprint radius ``r`` fits fully on-field.

    Erodes the on-field mask by ``r`` so the whole ball (not just its centre) lands on
    grass. Returns ``None`` if no such location exists (caller then skips the paste and
    keeps the crop as a negative rather than pasting a ball-on-black).
    """
    k = 2 * int(r) + 1
    er = cv2.erode(mask.astype(np.uint8), np.ones((k, k), np.uint8))
    ys, xs = np.where(er > 0)
    if len(xs) == 0:
        return None
    i = int(rng.integers(len(xs)))
    return float(xs[i]), float(ys[i])


def path_onfield(
    mask: np.ndarray,
    cx: float,
    cy: float,
    vel: tuple[float, float],
    n: int,
    r: int,
) -> bool:
    """True iff the ball footprint stays on-field at all ``n`` positions of the path.

    Frame ``i`` (0-based) sits ``(n-1-i)`` steps *before* the target frame, so the last
    frame is exactly at ``(cx, cy)`` and earlier frames are offset back along ``vel``.
    """
    h, w = mask.shape
    for i in range(n):
        px = cx - (n - 1 - i) * vel[0]
        py = cy - (n - 1 - i) * vel[1]
        ix, iy = int(round(px)), int(round(py))
        if ix - r < 0 or iy - r < 0 or ix + r >= w or iy + r >= h:
            return False
        if mask[iy, ix] == 0:
            return False
    return True


def _motion_blur(
    patch: np.ndarray, alpha: np.ndarray, vx: float, vy: float
) -> tuple[np.ndarray, np.ndarray]:
    """Directional blur of ~one frame of travel along ``(vx, vy)`` (capped).

    A real moving ball smears by roughly its per-frame displacement; static / slow balls
    are left sharp. Blurs alpha too so the feather matches the streaked ball.
    """
    speed = float(np.hypot(vx, vy))
    if speed < 1.5:
        return patch, alpha
    L = int(np.clip(round(speed), 3, 9))
    L |= 1  # odd
    ang = float(np.arctan2(vy, vx))
    k = np.zeros((L, L), np.float32)
    c = (L - 1) / 2.0
    for t in np.linspace(-(L // 2), L // 2, L * 3):
        x = int(round(c + t * np.cos(ang)))
        y = int(round(c + t * np.sin(ang)))
        if 0 <= x < L and 0 <= y < L:
            k[y, x] = 1.0
    s = float(k.sum())
    if s == 0:
        return patch, alpha
    k /= s
    return cv2.filter2D(patch, -1, k), cv2.filter2D(alpha, -1, k)


def estimate_ball_velocity(
    patch: np.ndarray,
    alpha: np.ndarray,
    rng: np.random.Generator,
    max_speed: float = 30.0,
    jitter_deg: float = 12.0,
) -> tuple[float, float]:
    """Infer the ball's per-frame velocity FROM its own motion blur (camera-physics rule).

    A moving ball is smeared into a streak along its velocity; a slow/static ball is a
    round disc. So the dark blob's principal-axis *orientation* is the motion direction and
    its *excess length* over the perpendicular (disc) width is the per-frame displacement.
    Reading velocity from the real appearance keeps the pasted ball's motion consistent with
    how it actually looks — instead of bolting a random velocity onto a real streak (which
    trains the detector on impossible "streaked one way, moving another" balls; EXP-26).

    Returns ``(vx, vy)`` in band px/frame. A round / undetectable blob -> a small slow
    velocity in a random direction (a near-static ball reads the same from any direction).
    The streak axis is 180°-ambiguous, so the sign is random; a small angular jitter is added.
    """
    border = patch[alpha < 0.1]
    if border.size < 10:
        return (0.0, 0.0)
    thr = float(np.median(border)) - 0.6 * (float(border.std()) + 1.0)
    ys, xs = np.where((patch < thr) & (alpha > 0.2))
    if len(xs) < 6:
        sp = float(abs(rng.normal(0.0, 2.0)))
        a0 = float(rng.uniform(0, 2 * np.pi))
        return (sp * np.cos(a0), sp * np.sin(a0))
    pts = np.stack([xs, ys], 1).astype(np.float64)
    pts -= pts.mean(0)
    evals, evecs = np.linalg.eigh((pts.T @ pts) / len(pts))  # ascending
    major = evecs[:, 1]
    pj = pts @ major
    pn = pts @ evecs[:, 0]
    speed = float(
        np.clip((pj.max() - pj.min()) - (pn.max() - pn.min()), 0.0, max_speed)
    )
    ang = float(np.arctan2(major[1], major[0]))
    if rng.random() < 0.5:
        ang += np.pi
    ang += float(np.deg2rad(rng.normal(0.0, jitter_deg)))
    return (speed * np.cos(ang), speed * np.sin(ang))


def erase_ball(stack: np.ndarray, bx: float, by: float, r: float) -> np.ndarray:
    """Inpaint a ball OUT of every frame of a substrate (modified in place).

    An unerased real ball in a paste substrate is an **unlabelled positive** — a distractor
    that teaches the detector to suppress real balls. Erasing the known game ball (a disc of
    radius ``r`` at ``(bx, by)``, big enough to cover its small inter-frame motion) before
    pasting the augmented ball guarantees the crop holds **exactly one** ball: the labelled
    one. Grass texture is filled by Telea inpainting.
    """
    h, w = stack.shape[1:]
    bx, by = int(round(bx)), int(round(by))
    # Inpaint only a local window (the hole is small) — a full-frame Telea inpaint is ~150ms
    # vs ~5ms here. ``inpaintRadius`` is the fill NEIGHBOURHOOD (small, ~3), NOT the hole size.
    pad = int(r) + 4
    x0, y0 = max(0, bx - pad), max(0, by - pad)
    x1, y1 = min(w, bx + pad), min(h, by + pad)
    if x1 <= x0 or y1 <= y0:
        return stack
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.circle(mask, (bx - x0, by - y0), int(r), 255, -1)
    for i in range(stack.shape[0]):
        roi = stack[i, y0:y1, x0:x1].copy()
        stack[i, y0:y1, x0:x1] = cv2.inpaint(roi, mask, 3, cv2.INPAINT_TELEA)
    return stack


def augment_crop_with_ball(
    stack: np.ndarray,
    patch: np.ndarray,
    alpha: np.ndarray,
    cx: float,
    cy: float,
    vel: tuple[float, float] = (0.0, 0.0),
    scale: float = 1.0,
    contrast: float = 1.0,
    flip: bool = False,
    blur: bool = False,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Cut-paste a real ball onto a ``(n, H, W)`` gray training stack as a far-ball positive.

    Places the ball along a **coherent straight path** at constant ``vel`` (band px/frame):
    the LAST frame sits exactly on ``(cx, cy)`` (the label / frame ``t``), and each earlier
    frame ``i`` is offset backwards by ``(n-1-i)·vel`` — continuous, no-teleport motion the
    detector can read, instead of a frozen or jittered sprite. ``vel`` should come from
    :func:`estimate_ball_velocity` so the motion matches the patch's real blur; the real
    blur is left intact (``blur=False`` by default — synthetic blur on an already-streaked
    patch double-blurs and contradicts the velocity, EXP-26). A horizontal ``flip`` mirrors
    the patch **and** the x-velocity so appearance and motion stay aligned. The input stack
    is not modified.

    The caller samples ``(cx, cy)`` on-field with enough margin for the whole motion path
    (:func:`sample_onfield_location`), and should :func:`erase_ball` any real ball from the
    substrate first. The target for training is ``(cx, cy)``.
    """
    out = stack.copy()
    n = out.shape[0]
    p = patch.copy()
    a = alpha.copy()
    vx, vy = vel
    if flip:
        p = p[:, ::-1].copy()
        a = a[:, ::-1].copy()
        vx = -vx  # mirror motion with appearance
    if blur:
        p, a = _motion_blur(p, a, vx, vy)
    for i in range(n):
        px = cx - (n - 1 - i) * vx
        py = cy - (n - 1 - i) * vy
        paste_ball(out[i], p, a, px, py, scale=scale, contrast=contrast, flip=False)
    return out


def occlude_ball(
    stack: np.ndarray,
    bx: float,
    by: float,
    r: float,
    frac: float = 0.5,
    rng: np.random.Generator | None = None,
    level: float | None = None,
) -> np.ndarray:
    """Cover ~``frac`` of the ball with a foreground occluder (modified in place).

    A RECALL/temporal-continuity hard positive: track DROPOUTS — the gaps that break the
    re-ranker's trajectory — happen mostly when a player passes in front of the ball. Pasting
    a foreground occluder (a dark limb/foot/shadow) over part of the still-labelled ball
    teaches the detector to keep firing through partial occlusion, so the ball stays in the
    candidate set across consecutive frames. The label stays at ``(bx, by)``.
    """
    rng = rng or np.random.default_rng()
    bx, by = int(round(bx)), int(round(by))
    rr = max(2, int(round(r)))
    lvl = (
        float(rng.uniform(25, 70)) if level is None else float(level)
    )  # dark foreground
    ang = float(rng.uniform(0, 2 * np.pi))
    nx, ny = float(np.cos(ang)), float(np.sin(ang))
    d = (1.0 - 2.0 * float(frac)) * rr  # chord offset: frac=0.5 -> half the disc
    h, w = stack.shape[1:]
    y0, y1 = max(0, by - rr), min(h, by + rr + 1)
    x0, x1 = max(0, bx - rr), min(w, bx + rr + 1)
    if y1 <= y0 or x1 <= x0:
        return stack
    ys, xs = np.mgrid[y0:y1, x0:x1]
    cover = (((xs - bx) ** 2 + (ys - by) ** 2) <= rr * rr) & (
        ((xs - bx) * nx + (ys - by) * ny) >= d
    )
    for i in range(stack.shape[0]):
        sub = stack[i, y0:y1, x0:x1]
        sub[cover] = int(lvl)
    return stack


def dim_ball(
    stack: np.ndarray, bx: float, by: float, r: float, factor: float = 0.5
) -> np.ndarray:
    """Reduce the ball's contrast toward the surrounding grass (modified in place).

    A RECALL hard positive: 72% of real far balls are *fainter than the local grass texture*
    (EXP-29). Pushing a pasted/real ball's contrast toward the grass level (``factor`` < 1)
    manufactures the faintest balls the detector keeps missing, teaching it to fire on them
    so the track does not drop out on dim frames. The label stays at ``(bx, by)``.
    """
    bx, by = int(round(bx)), int(round(by))
    rr = max(2, int(round(r)))
    h, w = stack.shape[1:]
    y0, y1 = max(0, by - rr), min(h, by + rr + 1)
    x0, x1 = max(0, bx - rr), min(w, bx + rr + 1)
    if y1 <= y0 or x1 <= x0:
        return stack
    gy0, gy1 = max(0, by - 2 * rr), min(h, by + 2 * rr)
    gx0, gx1 = max(0, bx - 2 * rr), min(w, bx + 2 * rr)
    grass = float(np.median(stack[0, gy0:gy1, gx0:gx1]))
    ys, xs = np.mgrid[y0:y1, x0:x1]
    disc = ((xs - bx) ** 2 + (ys - by) ** 2) <= rr * rr
    for i in range(stack.shape[0]):
        sub = stack[i, y0:y1, x0:x1].astype(np.float32)
        sub = np.where(disc, (sub - grass) * factor + grass, sub)
        stack[i, y0:y1, x0:x1] = np.clip(sub, 0, 255).astype(np.uint8)
    return stack


def patch_is_clean(
    patch: np.ndarray,
    alpha: np.ndarray,
    max_border_std: float = 22.0,
    min_contrast: float = 4.0,
    max_contrast: float = 90.0,
) -> bool:
    """True if a cropped patch is an ISOLATED ball (grass around it, modest contrast).

    Filters cut-paste *source* patches so the augmenter pastes only clean balls, not
    a ball stuck to a player's foot / a white shirt / a line. Heuristic: the feathered
    border ring (``alpha < 0.1``) should be **uniform grass** (low std) and the ball
    centre should differ from the grass by a *modest* amount — a big contrast means
    bright clutter (kit, tent), near-zero means no ball. On clip-1 this kept 21/73 of
    the labelled crops (EXP-23); the rest had a nearby player/marker in frame.
    """
    border = patch[alpha < 0.1]
    centre = patch[alpha > 0.8]
    if border.size < 20 or centre.size < 5:
        return False
    contrast = abs(float(centre.mean()) - float(border.mean()))
    return (
        float(border.std()) < max_border_std and min_contrast < contrast < max_contrast
    )


def sample_field_locations(
    field_mask: np.ndarray, n: int, rng: np.random.Generator
) -> list[tuple[int, int]]:
    """Sample ``n`` random ``(bx, by)`` band locations inside ``field_mask`` (>0)."""
    ys, xs = np.where(field_mask > 0)
    if len(xs) == 0:
        return []
    idx = rng.integers(0, len(xs), size=n)
    return [(int(xs[i]), int(ys[i])) for i in idx]
