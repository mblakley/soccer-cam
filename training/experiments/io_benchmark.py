"""I/O benchmark gate for the v4 warped-frame ball detector.

The gate the v4 rollout must clear BEFORE any long training run: prove the GPU
stays fed (>80% util) by a persistent-worker DataLoader streaming pre-decoded
warped-frame shards (training/data_prep/warped_pack.py) — instead of the old
tile path that sat at 0% util (JPEG decode + workers=0).

What it measures (all at the REAL warped frame size, swept over target_width):
  1. data-only   — DataLoader throughput with no GPU work (CPU/disk ceiling,
                   hardware-independent -> predicts whether the faster 4070 stays fed).
  2. compute-only — yolo26l forward+backward on synthetic in-VRAM batches
                   (images/s, ms/iter). Adaptive batch: descends until it fits 6 GB.
  3. end-to-end   — real loader + forward+backward, with live nvidia-smi util sampling.

Then a bottleneck verdict (data- vs compute-bound), per-iteration timing, a
projected time/epoch, and a 4070 extrapolation. The dummy loss (sum of output
means) is a throughput/util proxy, NOT real detection training — we are gating
the data pipeline, not learning.

Run on the GPU server (CUDA local). Example:
    uv run python -m training.experiments.io_benchmark \
        --reolink-video "F:/Heat_2012s/2026.05.27 - vs Chili Vortex (away)/RecM09_...mp4" \
        --dahua-video   "F:/Flash_2013s/05.01.2024 - vs RNYFC (away)/flash-rnyfc-away-...mp4" \
        --out-dir G:/pipeline_work/test/v4_bench \
        --target-widths 3264 5120 7680 --max-frames 600 --steps 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from training.data_prep.field_warp import build_field_warp
from training.data_prep.warped_pack import (
    WarpedShardDataset,
    build_warped_shard,
    read_shard_info,
)

# Validated Reolink 05-27 ball-size gradient (EXPERIMENTS v2): far ~8.5px ->
# near ~33px across the field band. Drives the warp's size(row) curve.
REOLINK_SRC = (7680, 2160)
REOLINK_ROWS = np.array([552.0, 912.0, 1272.0, 1452.0])
REOLINK_SIZES = np.array([8.5, 11.75, 21.0, 33.2])


# ---------------------------------------------------------------------------
# GPU utilization sampler
# ---------------------------------------------------------------------------


class GpuSampler:
    """Background thread sampling `nvidia-smi` GPU util + memory every ~interval s."""

    def __init__(self, device: int = 0, interval: float = 0.5):
        self.device = device
        self.interval = interval
        self._util: list[float] = []
        self._mem: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _query(self) -> tuple[float, float] | None:
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    f"--id={self.device}",
                    "--query-gpu=utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode != 0:
                return None
            util, mem = out.stdout.strip().split(",")
            return float(util), float(mem)
        except Exception:
            return None

    def _loop(self):
        while not self._stop.is_set():
            s = self._query()
            if s is not None:
                self._util.append(s[0])
                self._mem.append(s[1])
            self._stop.wait(self.interval)

    def __enter__(self):
        self._util.clear()
        self._mem.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def summary(self) -> dict:
        if not self._util:
            return {"samples": 0}
        u = sorted(self._util)
        return {
            "samples": len(u),
            "util_mean": round(statistics.mean(u), 1),
            "util_p50": round(u[len(u) // 2], 1),
            "util_max": round(max(u), 1),
            "pct_time_over_80": round(100 * sum(1 for x in u if x > 80) / len(u), 1),
            "mem_used_max_mb": round(max(self._mem), 0) if self._mem else 0,
        }


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class ConfigResult:
    target_width: int
    storage: str
    frame_shape: tuple[int, int, int]
    disk_mb_per_frame: float
    n_frames: int
    data_only_img_s: float = 0.0
    compute_batch: int = 0
    compute_img_s: float = 0.0
    compute_ms_iter: float = 0.0
    compute_peak_mb: float = 0.0
    e2e_img_s: float = 0.0
    e2e_ms_iter: float = 0.0
    e2e_data_wait_ms: float = 0.0
    workers: int = 0
    prefetch: int = 0
    gpu: dict = field(default_factory=dict)
    bottleneck: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dummy_loss(out):
    """Sum of means over all float tensors in a (possibly nested) model output.

    A throughput/util proxy that still backprops through the whole network.
    """
    import torch

    total = None

    def collect(o):
        nonlocal total
        if isinstance(o, torch.Tensor):
            if o.is_floating_point():
                m = o.mean()
                total = m if total is None else total + m
        elif isinstance(o, list | tuple):
            for x in o:
                collect(x)
        elif isinstance(o, dict):
            for x in o.values():
                collect(x)

    collect(out)
    if total is None:
        raise RuntimeError("no float tensors in model output to build a loss from")
    return total


def _load_net(model_path: str, device: str):
    from ultralytics import YOLO

    net = YOLO(model_path).model
    net = net.to(device)
    net.train()
    # YOLO(path) loads for inference with requires_grad=False on all params, so a
    # forward output has no grad graph and backward fails. Re-enable grad so the
    # benchmark exercises a real forward+backward.
    for p in net.parameters():
        p.requires_grad_(True)
    return net


def compute_only(net, shape, device, batch_candidates, steps, warmup=5) -> dict:
    """yolo26l forward+backward on synthetic batches. Adaptive batch for 6 GB."""
    import torch

    h, w, c = shape
    opt = torch.optim.SGD(net.parameters(), lr=1e-4)
    for batch in batch_candidates:
        try:
            torch.cuda.reset_peak_memory_stats(device)
            x = torch.rand(batch, c, h, w, device=device)
            # warmup
            for _ in range(warmup):
                opt.zero_grad(set_to_none=True)
                loss = _dummy_loss(net(x))
                loss.backward()
                opt.step()
            torch.cuda.synchronize(device)
            t0 = time.perf_counter()
            for _ in range(steps):
                opt.zero_grad(set_to_none=True)
                loss = _dummy_loss(net(x))
                loss.backward()
                opt.step()
            torch.cuda.synchronize(device)
            dt = time.perf_counter() - t0
            peak = torch.cuda.max_memory_allocated(device) / 1e6
            return {
                "batch": batch,
                "img_s": round(batch * steps / dt, 1),
                "ms_iter": round(1000 * dt / steps, 1),
                "peak_mb": round(peak, 0),
            }
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            continue
        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                torch.cuda.empty_cache()
                continue
            raise
    return {"batch": 0, "img_s": 0.0, "ms_iter": 0.0, "peak_mb": 0.0, "oom": True}


def data_only(loader, steps) -> float:
    """Sustained images/s the DataLoader delivers with no GPU work."""
    n = 0
    t0 = time.perf_counter()
    for i, batch in enumerate(loader):
        n += batch.shape[0]
        if i + 1 >= steps:
            break
    dt = time.perf_counter() - t0
    return round(n / dt, 1) if dt > 0 else 0.0


def end_to_end(net, loader, device, steps, warmup=5) -> dict:
    """Real loader + forward+backward; measures data-wait vs compute time."""
    import torch

    opt = torch.optim.SGD(net.parameters(), lr=1e-4)
    torch.cuda.reset_peak_memory_stats(device)
    n_img = 0
    data_wait = 0.0
    it = iter(loader)
    # warmup
    for _ in range(warmup):
        try:
            x = next(it).to(device, non_blocking=True)
        except StopIteration:
            it = iter(loader)
            x = next(it).to(device, non_blocking=True)
        opt.zero_grad(set_to_none=True)
        _dummy_loss(net(x)).backward()
        opt.step()
    torch.cuda.synchronize(device)
    t0 = time.perf_counter()
    for _ in range(steps):
        td = time.perf_counter()
        try:
            cpu = next(it)
        except StopIteration:
            it = iter(loader)
            cpu = next(it)
        x = cpu.to(device, non_blocking=True)
        data_wait += time.perf_counter() - td
        opt.zero_grad(set_to_none=True)
        _dummy_loss(net(x)).backward()
        opt.step()
        n_img += x.shape[0]
    torch.cuda.synchronize(device)
    dt = time.perf_counter() - t0
    return {
        "img_s": round(n_img / dt, 1),
        "ms_iter": round(1000 * dt / steps, 1),
        "data_wait_ms": round(1000 * data_wait / steps, 1),
        "peak_mb": round(torch.cuda.max_memory_allocated(device) / 1e6, 0),
    }


def build_reolink_shard(
    video, out_dir, target_width, storage, max_frames, frame_interval
):
    warp = build_field_warp(
        REOLINK_ROWS,
        REOLINK_SIZES,
        REOLINK_SRC[0],
        REOLINK_SRC[1],
        target_width=target_width,
    )
    name = f"reolink_tw{target_width}_{storage}"
    idx = Path(out_dir) / f"{name}.json"
    if idx.exists():
        return read_shard_info(idx)
    return build_warped_shard(
        video,
        warp,
        out_dir,
        name,
        frame_interval=frame_interval,
        max_frames=max_frames,
        storage=storage,
        game_id="heat__2026.05.27_vs_Chili_Vortex_away",
        camera="reolink",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="v4 warped-frame I/O benchmark gate")
    ap.add_argument("--reolink-video", type=Path, required=True)
    ap.add_argument("--dahua-video", type=Path, default=None)
    ap.add_argument(
        "--out-dir", type=Path, required=True, help="local SSD work dir (G:)"
    )
    ap.add_argument("--target-widths", type=int, nargs="+", default=[3264, 5120, 7680])
    ap.add_argument("--storage", choices=["raw", "compressed", "both"], default="both")
    ap.add_argument("--workers", type=int, nargs="+", default=[4, 8])
    ap.add_argument("--prefetch", type=int, nargs="+", default=[2, 4])
    ap.add_argument("--batch-candidates", type=int, nargs="+", default=[16, 8, 4, 2, 1])
    ap.add_argument("--max-frames", type=int, default=600)
    ap.add_argument("--frame-interval", type=int, default=8)
    ap.add_argument("--steps", type=int, default=150)
    ap.add_argument("--device", default="0")
    ap.add_argument("--model", default="yolo26l.pt")
    ap.add_argument(
        "--speedup-4070", type=float, default=3.5, help="4070/1060 compute factor"
    )
    ap.add_argument("--gate-util", type=float, default=80.0)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    storages = ["raw", "compressed"] if args.storage == "both" else [args.storage]

    import torch
    from torch.utils.data import DataLoader

    device = f"cuda:{args.device}" if torch.cuda.is_available() else "cpu"
    print(
        f"=== v4 I/O benchmark gate ===\ndevice={device} cuda={torch.cuda.is_available()}"
    )
    if device == "cpu":
        print(
            "!! CUDA NOT AVAILABLE — refusing to report a GPU gate (RDP-hidden-GPU trap?)."
        )
    net = _load_net(args.model, device)

    results: list[ConfigResult] = []
    for tw in args.target_widths:
        for storage in storages:
            info = build_reolink_shard(
                args.reolink_video,
                args.out_dir,
                tw,
                storage,
                args.max_frames,
                args.frame_interval,
            )
            shape = (info.frame_h, info.frame_w, info.channels)
            disk_mb = round(info.disk_bytes / max(info.n, 1) / 1e6, 2)
            print(
                f"\n--- TW={tw} storage={storage} shape={shape} "
                f"n={info.n} disk={disk_mb} MB/frame ---"
            )

            # compute-only (independent of loader config)
            comp = compute_only(net, shape, device, args.batch_candidates, args.steps)
            print(f"  compute-only: {comp}")

            if not comp["batch"]:
                # OOM at every batch size on this GPU. Still report the GPU-free
                # data-only throughput (hardware-independent), then skip the
                # end-to-end/util loop (can't run forward+backward here).
                nw0, pf0 = args.workers[0], args.prefetch[0]
                ds = WarpedShardDataset([info.path])
                loader = DataLoader(
                    ds,
                    batch_size=1,
                    num_workers=nw0,
                    persistent_workers=nw0 > 0,
                    pin_memory=True,
                    prefetch_factor=pf0 if nw0 > 0 else None,
                    shuffle=True,
                    drop_last=True,
                )
                d_img_s = data_only(loader, args.steps)
                ds.close()
                cr = ConfigResult(
                    target_width=tw,
                    storage=storage,
                    frame_shape=shape,
                    disk_mb_per_frame=disk_mb,
                    n_frames=info.n,
                    data_only_img_s=d_img_s,
                    workers=nw0,
                    prefetch=pf0,
                    bottleneck="compute-OOM",
                )
                cr.notes.append(
                    "yolo26l OOM at all batch sizes on this GPU; data-only only"
                )
                results.append(cr)
                print(
                    f"  OOM at all batch sizes on this GPU — data-only={d_img_s} img/s; "
                    "skipping end-to-end (needs larger-VRAM GPU or lower TW)."
                )
                continue

            for nw in args.workers:
                for pf in args.prefetch:
                    ds = WarpedShardDataset([info.path])
                    loader = DataLoader(
                        ds,
                        batch_size=comp["batch"] or 1,
                        num_workers=nw,
                        persistent_workers=nw > 0,
                        pin_memory=True,
                        prefetch_factor=pf if nw > 0 else None,
                        shuffle=True,
                        drop_last=True,
                    )
                    d_img_s = data_only(loader, args.steps)
                    with GpuSampler(int(args.device)) as samp:
                        e2e = end_to_end(net, loader, device, args.steps)
                    gpu = samp.summary()
                    cr = ConfigResult(
                        target_width=tw,
                        storage=storage,
                        frame_shape=shape,
                        disk_mb_per_frame=disk_mb,
                        n_frames=info.n,
                        data_only_img_s=d_img_s,
                        compute_batch=comp["batch"],
                        compute_img_s=comp["img_s"],
                        compute_ms_iter=comp["ms_iter"],
                        compute_peak_mb=comp["peak_mb"],
                        e2e_img_s=e2e["img_s"],
                        e2e_ms_iter=e2e["ms_iter"],
                        e2e_data_wait_ms=e2e["data_wait_ms"],
                        workers=nw,
                        prefetch=pf,
                        gpu=gpu,
                    )
                    cr.bottleneck = (
                        "data-bound"
                        if d_img_s < comp["img_s"] * 0.95
                        else "compute-bound"
                    )
                    # 4070 extrapolation: it finishes a step ~speedup x faster, so
                    # it needs ~speedup x the feed rate to stay busy.
                    needed_4070 = comp["img_s"] * args.speedup_4070
                    if d_img_s < needed_4070:
                        cr.notes.append(
                            f"4070 risk: data {d_img_s} img/s < needed ~{round(needed_4070)} img/s"
                        )
                    results.append(cr)
                    loader = None
                    ds.close()
                    print(
                        f"  nw={nw} pf={pf}: data={d_img_s} e2e={e2e['img_s']} img/s "
                        f"util={gpu.get('util_mean')}% (>80%: {gpu.get('pct_time_over_80')}%) "
                        f"wait={e2e['data_wait_ms']}ms [{cr.bottleneck}]"
                    )

    # ---- report ----
    best = max(results, key=lambda r: r.gpu.get("util_mean", 0)) if results else None
    summary = {
        "device": device,
        "cuda": torch.cuda.is_available(),
        "model": args.model,
        "gate_util": args.gate_util,
        "speedup_4070": args.speedup_4070,
        "results": [r.__dict__ for r in results],
        "best": best.__dict__ if best else None,
        "gate_pass": bool(
            best
            and torch.cuda.is_available()
            and best.gpu.get("util_mean", 0) > args.gate_util
        ),
    }
    out_json = args.out_dir / "io_benchmark_results.json"
    out_json.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n=== SUMMARY (written to {out_json}) ===")
    if best:
        print(
            f"best config: TW={best.target_width} storage={best.storage} "
            f"nw={best.workers} pf={best.prefetch} batch={best.compute_batch}"
        )
        print(
            f"  GPU util mean={best.gpu.get('util_mean')}% "
            f"p50={best.gpu.get('util_p50')}% >80%={best.gpu.get('pct_time_over_80')}%"
        )
        print(
            f"  e2e {best.e2e_img_s} img/s, {best.e2e_ms_iter} ms/iter, "
            f"data-wait {best.e2e_data_wait_ms} ms, peak {best.compute_peak_mb} MB"
        )
        print(f"  bottleneck: {best.bottleneck}")
        print(
            f"GATE {'PASS' if summary['gate_pass'] else 'FAIL'} "
            f"(threshold mean util > {args.gate_util}%)"
        )


if __name__ == "__main__":
    main()
