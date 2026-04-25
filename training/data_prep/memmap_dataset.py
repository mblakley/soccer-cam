"""Memory-mapped YOLO dataset for fast training data loading.

Overrides load_image to read from a pre-built binary cache (.bin) via
np.memmap instead of individual image files. Workers lazy-init their
own memmap handles, so this works with multiprocessing (spawn on Windows).

This module is imported by worker processes — no monkey-patching needed.
"""

import numpy as np
from ultralytics.data.dataset import YOLODataset

TILE_H, TILE_W, TILE_C = 640, 640, 3


class MemmapYOLODataset(YOLODataset):
    """YOLODataset that loads images from a memory-mapped binary cache."""

    def load_image(self, i, rect_mode=True):
        """Load image from memmap binary cache with lazy-init per worker."""
        # Check YOLO's own RAM buffer first
        im = self.ims[i]
        if im is not None:
            return self.ims[i], self.im_hw0[i], self.im_hw[i]

        # Check if this dataset has memmap config
        memmap_idx = getattr(self, "_memmap_idx", None)
        if memmap_idx is None:
            return super().load_image(i, rect_mode)

        f = self.im_files[i]
        if f not in memmap_idx:
            return super().load_image(i, rect_mode)

        # Lazy-init memmap handle (each worker process opens its own)
        mmap_handles = getattr(self, "_mmap_handles", None)
        if mmap_handles is None:
            self._mmap_handles = {}
            mmap_handles = self._mmap_handles

        split = self._memmap_split[f]
        if split not in mmap_handles:
            bin_path, n = self._memmap_bin_info[split]
            mmap_handles[split] = np.memmap(
                bin_path,
                dtype=np.uint8,
                mode="r",
                shape=(n, TILE_H, TILE_W, TILE_C),
            )

        idx = memmap_idx[f]
        im = np.array(mmap_handles[split][idx])  # copy for augmentation safety

        h0, w0 = TILE_H, TILE_W

        # Replicate YOLO's buffer management
        if self.augment:
            self.ims[i] = im
            self.im_hw0[i] = (h0, w0)
            self.im_hw[i] = (h0, w0)
            self.buffer.append(i)
            if 1 < len(self.buffer) >= self.max_buffer_length:
                j = self.buffer.pop(0)
                if self.cache != "ram":
                    self.ims[j], self.im_hw0[j], self.im_hw[j] = None, None, None

        return im, (h0, w0), (h0, w0)
