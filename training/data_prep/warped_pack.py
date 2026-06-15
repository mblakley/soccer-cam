"""Pre-decoded warped-frame shards for the v4 full-frame ball detector.

v4 trains on **warped full frames** (one image per sampled frame), not 21
native tiles. The prior tile path was GPU-starved (0% util) because every tile
was a JPEG decoded on the fly with a non-persistent DataLoader. This module
removes decode from the train-time hot path by warping each sampled frame
**once, offline** (via :func:`field_warp.warp_frame`) and writing it into a
sequential shard that a persistent-worker DataLoader streams with no per-sample
JPEG decode (raw mode) or with a single cheap warped-frame decode (compressed
mode).

Two storage modes (the I/O benchmark compares them at the real warped size):

- ``raw``  — one contiguous ``(n, H, W, 3)`` uint8 memmap (``.dat``). Zero decode
  at train time; ~``H*W*3`` bytes/frame (~3.6-8 MB at TW 5120-7680).
- ``compressed`` — n encoded blobs concatenated (``.blobs``) with per-frame
  offsets in the index. ~5-10x smaller on disk; one ``cv2.imdecode`` per sample
  (still far cheaper than the old 21-tile / full-panorama decode).

Layout per shard ``<name>``:
- ``<name>.json``  — index (storage mode, dims, n, src frame indices, warp meta,
  and blob offsets for compressed mode).
- ``<name>.dat``   — raw mode pixel data (memmap).
- ``<name>.blobs`` — compressed mode encoded bytes.

The shard write/read core is pure numpy (+ cv2 for compressed), so it is
unit-testable without torch, a GPU, or a real video. :func:`build_warped_shard`
adds video decoding (PyAV) on top; :class:`WarpedShardDataset` adds the torch
wrapper. Both import their heavy deps lazily so this module loads anywhere.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from training.data_prep.field_warp import FieldWarp, warp_frame

# YOLO/most detectors require input dims divisible by the max stride (32).
STRIDE = 32
DEFAULT_JPEG_QUALITY = 92


def _pad_to_stride(n: int, stride: int = STRIDE) -> int:
    """Round ``n`` up to the next multiple of ``stride``."""
    return int(np.ceil(n / stride) * stride)


def warp_meta_from(warp: FieldWarp) -> dict:
    """Scalar warp metadata stored in the index (not the full remap maps).

    Enough to reconstruct dimensions and reason about the warp; the full inverse
    (``unwarp_points``) is an inference-time concern handled from the live
    :class:`FieldWarp`, not the shard.
    """
    return {
        "src_w": int(warp.src_w),
        "src_h": int(warp.src_h),
        "y_top": int(warp.y_top),
        "y_bot": int(warp.y_bot),
        "out_h": int(warp.out_h),
        "target_width": int(warp.target_width),
        "final_h": int(warp.final_h),
        "target_size": float(warp.target_size),
    }


@dataclass(frozen=True)
class ShardInfo:
    """Parsed shard index."""

    path: Path  # the .json index path
    storage: str  # "raw" | "compressed"
    n: int
    frame_h: int
    frame_w: int
    channels: int
    game_id: str | None
    camera: str | None
    target_width: int
    frame_indices: list[int]
    blob_offsets: list[tuple[int, int]]  # (offset, size) per frame, compressed only
    warp: dict

    @property
    def data_path(self) -> Path:
        suffix = ".dat" if self.storage == "raw" else ".blobs"
        return self.path.with_suffix(suffix)

    @property
    def bytes_per_frame_raw(self) -> int:
        return self.frame_h * self.frame_w * self.channels

    @property
    def disk_bytes(self) -> int:
        return self.data_path.stat().st_size if self.data_path.exists() else 0


# ---------------------------------------------------------------------------
# Writer (pure numpy + cv2; no torch, no video)
# ---------------------------------------------------------------------------


def write_shard(
    frames: Iterable[np.ndarray],
    out_dir: Path | str,
    shard_name: str,
    *,
    storage: str = "raw",
    frame_indices: Iterable[int] | None = None,
    game_id: str | None = None,
    camera: str | None = None,
    target_width: int | None = None,
    warp_meta: dict | None = None,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    pad_to_stride: bool = True,
) -> ShardInfo:
    """Write a sequence of (already-warped) HWC uint8 frames into one shard.

    All frames must share a shape. ``raw`` writes a contiguous uint8 blob
    streamed to disk (never all frames in RAM at once). ``compressed`` PNG-
    encodes each frame (lossless, so round-trips exactly) and concatenates the
    bytes with per-frame offsets recorded in the index.

    Args:
        frames: iterable of HWC uint8 arrays (e.g. from :func:`warp_frame`).
        out_dir: directory for ``<shard_name>.{json,dat,blobs}``.
        shard_name: base name (no extension).
        storage: ``"raw"`` or ``"compressed"``.
        frame_indices: optional source frame index per frame (for traceability).
        game_id, camera, target_width, warp_meta: stored in the index.
        jpeg_quality: ignored (compressed mode uses lossless PNG for round-trip).
        pad_to_stride: pad H and W up to a multiple of :data:`STRIDE` so frames
            feed a detector directly. Padding is bottom/right with zeros.

    Returns:
        The :class:`ShardInfo` for the written shard.
    """
    if storage not in ("raw", "compressed"):
        raise ValueError(f"storage must be 'raw' or 'compressed', got {storage!r}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / f"{shard_name}.json"
    data_path = out_dir / (
        f"{shard_name}.dat" if storage == "raw" else f"{shard_name}.blobs"
    )

    shape: tuple[int, int, int] | None = None
    n = 0
    blob_offsets: list[tuple[int, int]] = []
    offset = 0

    if storage == "compressed":
        import cv2

    with open(data_path, "wb") as fh:
        for idx, frame in enumerate(frames):
            arr = np.ascontiguousarray(frame)
            if arr.ndim != 3 or arr.shape[2] not in (1, 3):
                raise ValueError(
                    f"frame {idx} must be HWC with 1 or 3 channels, got {arr.shape}"
                )
            if arr.dtype != np.uint8:
                raise ValueError(f"frame {idx} must be uint8, got {arr.dtype}")

            if pad_to_stride:
                h, w = arr.shape[:2]
                hp, wp = _pad_to_stride(h), _pad_to_stride(w)
                if (hp, wp) != (h, w):
                    padded = np.zeros((hp, wp, arr.shape[2]), dtype=np.uint8)
                    padded[:h, :w] = arr
                    arr = padded

            if shape is None:
                shape = arr.shape
            elif arr.shape != shape:
                raise ValueError(
                    f"all frames must share a shape; frame {idx} is {arr.shape}, expected {shape}"
                )

            if storage == "raw":
                fh.write(arr.tobytes())
            else:
                ok, enc = cv2.imencode(".png", arr)
                if not ok:
                    raise RuntimeError(f"PNG encode failed for frame {idx}")
                buf = enc.tobytes()
                fh.write(buf)
                blob_offsets.append((offset, len(buf)))
                offset += len(buf)
            n += 1

    if shape is None:
        # No frames written — clean up the empty data file.
        data_path.unlink(missing_ok=True)
        raise ValueError("no frames provided")

    # Snapshot indices AFTER consuming frames, so callers may pass a list that is
    # populated as a side effect of the frame generator (build_warped_shard does).
    if frame_indices is not None:
        indices = list(frame_indices)
        if len(indices) != n:
            raise ValueError(f"frame_indices length {len(indices)} != frame count {n}")
    else:
        indices = list(range(n))

    frame_h, frame_w, channels = int(shape[0]), int(shape[1]), int(shape[2])
    info = {
        "version": 1,
        "storage": storage,
        "n": n,
        "frame_h": frame_h,
        "frame_w": frame_w,
        "channels": channels,
        "dtype": "uint8",
        "game_id": game_id,
        "camera": camera,
        "target_width": int(target_width) if target_width is not None else frame_w,
        "frame_indices": indices,
        "blob_offsets": blob_offsets,
        "warp": warp_meta or {},
    }
    index_path.write_text(json.dumps(info))
    return read_shard_info(index_path)


def read_shard_info(index_path: Path | str) -> ShardInfo:
    """Parse a shard ``.json`` index into a :class:`ShardInfo`."""
    index_path = Path(index_path)
    d = json.loads(index_path.read_text())
    return ShardInfo(
        path=index_path,
        storage=d["storage"],
        n=int(d["n"]),
        frame_h=int(d["frame_h"]),
        frame_w=int(d["frame_w"]),
        channels=int(d["channels"]),
        game_id=d.get("game_id"),
        camera=d.get("camera"),
        target_width=int(d.get("target_width", d["frame_w"])),
        frame_indices=list(d.get("frame_indices", list(range(int(d["n"]))))),
        blob_offsets=[tuple(o) for o in d.get("blob_offsets", [])],
        warp=d.get("warp", {}),
    )


# ---------------------------------------------------------------------------
# Reader (pure numpy + cv2; no torch)
# ---------------------------------------------------------------------------


class WarpedShard:
    """Random-access reader over one shard. Torch-free.

    ``raw`` mode memmaps the ``.dat`` so reads are zero-copy page-cache hits.
    ``compressed`` mode keeps the offsets and decodes one blob per ``get_frame``.
    """

    def __init__(self, index_path: Path | str):
        self.info = read_shard_info(index_path)
        self._mm: np.ndarray | None = None
        self._fh = None
        if self.info.storage == "raw":
            self._mm = np.memmap(
                self.info.data_path,
                dtype=np.uint8,
                mode="r",
                shape=(
                    self.info.n,
                    self.info.frame_h,
                    self.info.frame_w,
                    self.info.channels,
                ),
            )
        else:
            self._fh = open(self.info.data_path, "rb")

    def __len__(self) -> int:
        return self.info.n

    def get_frame(self, i: int) -> np.ndarray:
        """Return frame ``i`` as a HWC uint8 array (a copy, safe to mutate)."""
        if i < 0 or i >= self.info.n:
            raise IndexError(i)
        if self.info.storage == "raw":
            return np.array(self._mm[i])  # copy out of the memmap
        import cv2

        offset, size = self.info.blob_offsets[i]
        self._fh.seek(offset)
        buf = self._fh.read(size)
        arr = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise RuntimeError(f"failed to decode frame {i} in {self.info.path}")
        if arr.ndim == 2:
            arr = arr[..., None]
        return arr

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._mm = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Video -> warped shard (PyAV; integration path, runs on the server)
# ---------------------------------------------------------------------------


def iter_video_frames(
    video_path: Path | str, frame_interval: int = 8, max_frames: int | None = None
) -> Iterator[tuple[int, np.ndarray]]:
    """Yield ``(src_frame_idx, BGR_uint8_frame)`` every ``frame_interval`` frames.

    Uses PyAV (not a subprocess ffmpeg CLI). Decodes sequentially and keeps every
    ``frame_interval``-th frame.
    """
    import av  # lazy: only needed for the integration path

    container = av.open(str(video_path))
    try:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        kept = 0
        for idx, frame in enumerate(container.decode(stream)):
            if idx % frame_interval != 0:
                continue
            bgr = frame.to_ndarray(format="bgr24")
            yield idx, bgr
            kept += 1
            if max_frames is not None and kept >= max_frames:
                break
    finally:
        container.close()


def build_warped_shard(
    video_path: Path | str,
    warp: FieldWarp,
    out_dir: Path | str,
    shard_name: str,
    *,
    frame_interval: int = 8,
    max_frames: int | None = None,
    storage: str = "raw",
    game_id: str | None = None,
    camera: str | None = None,
) -> ShardInfo:
    """Decode a video, warp each sampled frame once (offline), write a shard.

    The warp is applied here, never on the train-time hot path. ``warp`` carries
    ``target_width`` (the swept resolution knob), so the shard frame size
    reflects the real candidate v4 input size. Single decode pass: source frame
    indices are captured into a list as the generator runs and stored in the
    index for traceability.
    """
    indices: list[int] = []

    def _warped_frames():
        for idx, bgr in iter_video_frames(video_path, frame_interval, max_frames):
            indices.append(idx)
            yield warp_frame(bgr, warp)

    return write_shard(
        _warped_frames(),
        out_dir,
        shard_name,
        storage=storage,
        frame_indices=indices,  # populated by the generator; snapshot at the end
        game_id=game_id,
        camera=camera,
        target_width=warp.target_width,
        warp_meta=warp_meta_from(warp),
    )


# ---------------------------------------------------------------------------
# Torch Dataset + shard rotation (lazy torch import)
# ---------------------------------------------------------------------------


class WarpedShardDataset:
    """Torch ``Dataset`` over one or more warped shards.

    ``__getitem__`` returns a CHW float32 tensor in ``[0, 1]`` (RGB). For the
    I/O benchmark this is image-only; warped labels (Dahua mapped via
    ``warp_points`` + Reolink far-ball labels) are the downstream dataset-writer
    phase. Imports torch lazily so the rest of this module loads without it.
    """

    def __init__(self, index_paths: list[Path | str], *, to_rgb: bool = True):
        if not index_paths:
            raise ValueError("index_paths must be non-empty")
        # Store only picklable state in __init__ so the dataset survives the
        # Windows DataLoader spawn (open memmaps/file handles do NOT pickle).
        # Shards are opened lazily, once per worker process, on first access.
        self._index_paths = [str(p) for p in index_paths]
        self._to_rgb = to_rgb
        self._cum: list[int] = []
        total = 0
        for p in self._index_paths:
            total += read_shard_info(p).n
            self._cum.append(total)
        self._len = total
        self._open: dict[int, WarpedShard] = {}

    def __len__(self) -> int:
        return self._len

    def _locate(self, i: int) -> tuple[int, int]:
        if i < 0 or i >= self._len:
            raise IndexError(i)
        import bisect

        s = bisect.bisect_right(self._cum, i)
        prev = self._cum[s - 1] if s > 0 else 0
        return s, i - prev

    def _shard(self, s: int) -> WarpedShard:
        sh = self._open.get(s)
        if sh is None:
            sh = WarpedShard(self._index_paths[s])
            self._open[s] = sh
        return sh

    def __getitem__(self, i: int):
        import torch

        s, local = self._locate(i)
        arr = self._shard(s).get_frame(local)  # HWC BGR uint8
        if self._to_rgb and arr.shape[2] == 3:
            arr = arr[..., ::-1]
        chw = np.ascontiguousarray(arr.transpose(2, 0, 1))  # CHW
        return torch.from_numpy(chw).float().div_(255.0)

    def close(self) -> None:
        for s in self._open.values():
            s.close()
        self._open.clear()


class ShardRotator:
    """Prefetch the next shard from a serving tier (D:/F:) to a local SSD (G:).

    Copies the next shard's files on a background thread while the current shard
    trains, so the GPU never waits on a cold shard. Needed because the full
    warped set (hundreds of GB->1 TB at high TW) exceeds the local SSD, so only
    a bounded rolling working set is ever resident. Remote workers see only D:
    via SMB, so ``src_dir`` is a D: path and ``local_dir`` is their local SSD.
    """

    def __init__(
        self, shard_names: list[str], src_dir: Path | str, local_dir: Path | str
    ):
        self.shard_names = list(shard_names)
        self.src_dir = Path(src_dir)
        self.local_dir = Path(local_dir)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._thread = None

    def _copy_one(self, name: str) -> list[Path]:
        import shutil

        copied = []
        for suffix in (".json", ".dat", ".blobs"):
            src = self.src_dir / f"{name}{suffix}"
            if src.exists():
                dst = self.local_dir / f"{name}{suffix}"
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dst)
                copied.append(dst)
        return copied

    def ensure_local(self, name: str) -> Path:
        """Synchronously ensure shard ``name`` is on the local SSD; return its index path."""
        self._copy_one(name)
        return self.local_dir / f"{name}.json"

    def prefetch_async(self, name: str) -> None:
        """Start copying shard ``name`` in the background."""
        import threading

        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._thread = threading.Thread(
            target=self._copy_one, args=(name,), daemon=True
        )
        self._thread.start()

    def wait(self) -> None:
        if self._thread is not None:
            self._thread.join()
            self._thread = None
