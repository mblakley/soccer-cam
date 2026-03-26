"""PyTorch Dataset for 3-frame temporal ball detection.

Loads triplets of consecutive frames (prev, curr, next) and generates
Gaussian heatmap targets for ball center prediction. Used with the
temporal model (train_temporal.py).
"""

import json
import logging
from pathlib import Path

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset

logger = logging.getLogger(__name__)

DEFAULT_SIGMA = 2.0  # Gaussian heatmap sigma in pixels
TILE_SIZE = 640


def generate_heatmap(
    cx: float,
    cy: float,
    size: int = TILE_SIZE,
    sigma: float = DEFAULT_SIGMA,
) -> np.ndarray:
    """Generate a 2D Gaussian heatmap centered at (cx, cy).

    Args:
        cx: Normalized x coordinate [0, 1]
        cy: Normalized y coordinate [0, 1]
        size: Output heatmap size (square)
        sigma: Gaussian standard deviation in pixels

    Returns:
        (size, size) float32 array with values in [0, 1]
    """
    px = cx * size
    py = cy * size

    x = np.arange(size, dtype=np.float32)
    y = np.arange(size, dtype=np.float32)
    xx, yy = np.meshgrid(x, y)

    heatmap = np.exp(-((xx - px) ** 2 + (yy - py) ** 2) / (2 * sigma**2))
    return heatmap


class TemporalBallDataset(Dataset):
    """Dataset that loads 3-frame triplets for temporal ball detection.

    Each sample returns:
        images: (9, H, W) tensor — 3 RGB frames concatenated channel-wise
        heatmap: (1, H, W) tensor — Gaussian target (or zeros for negatives)
        meta: dict with file paths and label info
    """

    def __init__(
        self,
        manifest_path: str | Path,
        sigma: float = DEFAULT_SIGMA,
        augment: bool = True,
        img_size: int = TILE_SIZE,
    ):
        """Initialize dataset from a JSONL manifest.

        Args:
            manifest_path: Path to .jsonl manifest from create_temporal_dataset.py
            sigma: Gaussian sigma for heatmap generation
            augment: Whether to apply data augmentation
            img_size: Expected image size
        """
        self.sigma = sigma
        self.augment = augment
        self.img_size = img_size
        self.samples: list[dict] = []

        with open(manifest_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    self.samples.append(json.loads(line))

        logger.info(
            "Loaded %d samples from %s (%d positive, %d negative)",
            len(self.samples),
            manifest_path,
            sum(1 for s in self.samples if s["positive"]),
            sum(1 for s in self.samples if not s["positive"]),
        )

    def __len__(self) -> int:
        return len(self.samples)

    def _load_image(self, path: str) -> np.ndarray:
        """Load an image as RGB float32 [0, 1]."""
        img = cv2.imread(path)
        if img is None:
            # Return black image if file is missing
            logger.warning("Missing image: %s", path)
            return np.zeros((self.img_size, self.img_size, 3), dtype=np.float32)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = img.astype(np.float32) / 255.0
        # Resize if needed
        h, w = img.shape[:2]
        if h != self.img_size or w != self.img_size:
            img = cv2.resize(img, (self.img_size, self.img_size))
        return img

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor, dict]:
        sample = self.samples[idx]

        # Load 3 frames
        prev_img = self._load_image(sample["prev"])
        curr_img = self._load_image(sample["curr"])
        next_img = self._load_image(sample["next"])

        # Generate heatmap target
        if sample["positive"] and sample["cx"] is not None:
            cx, cy = sample["cx"], sample["cy"]
            heatmap = generate_heatmap(cx, cy, self.img_size, self.sigma)
        else:
            cx, cy = None, None
            heatmap = np.zeros((self.img_size, self.img_size), dtype=np.float32)

        # Augmentation
        if self.augment:
            # Horizontal flip (same transform to all 3 frames + heatmap)
            if np.random.random() < 0.5:
                prev_img = np.flip(prev_img, axis=1).copy()
                curr_img = np.flip(curr_img, axis=1).copy()
                next_img = np.flip(next_img, axis=1).copy()
                heatmap = np.flip(heatmap, axis=1).copy()
                if cx is not None:
                    cx = 1.0 - cx

            # Color jitter (same transform to all 3 frames)
            if np.random.random() < 0.8:
                # Brightness
                brightness = np.random.uniform(0.8, 1.2)
                prev_img = np.clip(prev_img * brightness, 0, 1)
                curr_img = np.clip(curr_img * brightness, 0, 1)
                next_img = np.clip(next_img * brightness, 0, 1)

                # Contrast
                contrast = np.random.uniform(0.8, 1.2)
                for img in [prev_img, curr_img, next_img]:
                    mean = img.mean()
                    img[:] = np.clip((img - mean) * contrast + mean, 0, 1)

        # Stack into 9-channel tensor: (3, H, W) per frame → (9, H, W)
        stacked = np.concatenate(
            [
                prev_img.transpose(2, 0, 1),  # (3, H, W)
                curr_img.transpose(2, 0, 1),
                next_img.transpose(2, 0, 1),
            ],
            axis=0,
        )  # (9, H, W)

        images = torch.from_numpy(stacked)
        heatmap_tensor = torch.from_numpy(heatmap).unsqueeze(0)  # (1, H, W)

        meta = {
            "curr_path": sample["curr"],
            "positive": sample["positive"],
            "cx": sample.get("cx"),
            "cy": sample.get("cy"),
        }

        return images, heatmap_tensor, meta
