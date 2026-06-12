"""Evaluate the student field-outline model against teacher labels.

Reports, on held-out venue clusters: per-point pixel error (768x384
space), polygon IoU vs the teacher, score-head MAE, and gate agreement
(does the student agree with the teacher on mean_score >= 0.70 — the
number that decides whether it is a safe drop-in). Breaks results down per
cluster and per team so the Heat-heavy corpus doesn't mask weak Flash
generalization, and renders teacher-vs-student overlay PNGs for visual QA.

Run on the GPU server / any ml environment.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from training.field_outline import GATE_THRESHOLD, INPUT_H, INPUT_W, NUM_KEYPOINTS
from training.field_outline.augment import COORD_SCORE_MIN, augment_sample
from training.field_outline.dataset import build_datasets, polygon_iou

logger = logging.getLogger(__name__)


def predict(model, sample, device) -> tuple[np.ndarray, np.ndarray]:
    """Run the student on one sample (val transform). Returns (kpts, scores)."""
    import torch

    bgr = cv2.imread(str(sample.jpg))
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    img, _, _, _ = augment_sample(
        rgb, sample.kpts, sample.scores, np.random.default_rng(0), train=False
    )
    x = torch.from_numpy(img.transpose(2, 0, 1).copy()).unsqueeze(0).to(device)
    with torch.no_grad():
        pk, ps = model(x)
    return pk[0].cpu().numpy(), ps[0].cpu().numpy()


def _draw(jpg: Path, teacher: np.ndarray, student: np.ndarray) -> np.ndarray:
    img = cv2.imread(str(jpg))
    h, w = img.shape[:2]
    for poly, color in ((teacher, (0, 255, 0)), (student, (0, 0, 255))):
        pts = (poly * [w, h]).astype(np.int32)
        cv2.polylines(img, [pts[:5]], False, color, 2)
        cv2.polylines(img, [pts[5:]], False, color, 2)
        for px, py in pts:
            cv2.circle(img, (int(px), int(py)), 4, color, -1)
    cv2.putText(
        img,
        "teacher=green student=red",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return img


def evaluate(model, samples, device) -> list[dict]:
    """Per-sample metrics vs teacher labels."""
    scale = np.array([INPUT_W, INPUT_H], dtype=np.float32)
    records = []
    for s in samples:
        pk, ps = predict(model, s, device)
        valid = (s.scores >= COORD_SCORE_MIN) & bool(s.gate_pass)
        per_pt_err = np.linalg.norm((pk - s.kpts) * scale, axis=1)  # (10,)
        stu_mean = float(ps.mean())
        records.append(
            {
                "cluster": s.cluster,
                "team": s.team,
                "venue": s.venue,
                "per_pt_err": per_pt_err,
                "valid": valid,
                "iou": polygon_iou(pk, s.kpts),
                "score_mae": float(np.abs(ps - s.scores).mean()),
                "gate_agree": (stu_mean >= GATE_THRESHOLD) == bool(s.gate_pass),
                "jpg": s.jpg,
                "teacher": s.kpts,
                "student": pk,
            }
        )
    return records


def _summary(records: list[dict]) -> dict:
    if not records:
        return {}
    errs = (
        np.concatenate(
            [r["per_pt_err"][r["valid"]] for r in records if r["valid"].any()]
        )
        if any(r["valid"].any() for r in records)
        else np.array([])
    )
    ious = np.array([r["iou"] for r in records])
    return {
        "n": len(records),
        "px_mean": float(errs.mean()) if errs.size else float("nan"),
        "px_p95": float(np.percentile(errs, 95)) if errs.size else float("nan"),
        "iou_mean": float(ious.mean()),
        "iou_ge90": float((ious >= 0.90).mean()),
        "iou_ge95": float((ious >= 0.95).mean()),
        "score_mae": float(np.mean([r["score_mae"] for r in records])),
        "gate_agree": float(np.mean([r["gate_agree"] for r in records])),
    }


def _print_table(title: str, groups: dict[str, list[dict]]) -> None:
    print(f"\n=== {title} ===")
    print(
        f"{'group':30} {'n':>5} {'px_mean':>8} {'px_p95':>7} "
        f"{'iou':>6} {'>=.90':>6} {'gate':>6}"
    )
    for name in sorted(groups):
        s = _summary(groups[name])
        if not s:
            continue
        print(
            f"{name[:30]:30} {s['n']:5} {s['px_mean']:8.2f} {s['px_p95']:7.2f} "
            f"{s['iou_mean']:6.3f} {s['iou_ge90']:6.2f} {s['gate_agree']:6.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate field-outline student")
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--split", default="test", choices=["val", "test", "train"])
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--overlays-dir", type=Path, default=None)
    parser.add_argument("--num-overlays", type=int, default=40)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    import torch

    from training.field_outline.model import FieldOutlineNet

    device = torch.device(
        args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    )
    ckpt = torch.load(args.checkpoint, map_location=device)
    model = FieldOutlineNet(ckpt.get("backbone", "resnet18"), pretrained=False)
    model.load_state_dict(ckpt["model"])
    model.to(device).eval()

    train_ds, val_ds, test_ds, _ = build_datasets(args.dataset_root, args.seed)
    ds = {"train": train_ds, "val": val_ds, "test": test_ds}[args.split]
    if not len(ds):
        logger.error("Split %r is empty", args.split)
        return

    records = evaluate(model, ds.samples, device)

    print(f"\n### Eval on '{args.split}' split (epoch {ckpt.get('epoch', '?')}) ###")
    overall = _summary(records)
    print(
        f"overall: n={overall['n']} px_mean={overall['px_mean']:.2f} "
        f"px_p95={overall['px_p95']:.2f} iou_mean={overall['iou_mean']:.3f} "
        f"iou>=.90={overall['iou_ge90']:.2f} iou>=.95={overall['iou_ge95']:.2f} "
        f"score_mae={overall['score_mae']:.3f} gate_agree={overall['gate_agree']:.2f}"
    )

    by_team = defaultdict(list)
    by_cluster = defaultdict(list)
    for r in records:
        by_team[r["team"]].append(r)
        by_cluster[r["cluster"]].append(r)
    _print_table("per team", by_team)
    _print_table("per cluster", by_cluster)

    # Per-point error (which corners lag)
    pp = np.zeros(NUM_KEYPOINTS)
    cnt = np.zeros(NUM_KEYPOINTS)
    for r in records:
        pp += np.where(r["valid"], r["per_pt_err"], 0.0)
        cnt += r["valid"]
    print(
        "\nper-point px error:",
        " ".join(
            f"{i}:{pp[i] / cnt[i]:.1f}" if cnt[i] else f"{i}:--"
            for i in range(NUM_KEYPOINTS)
        ),
    )

    if args.overlays_dir:
        args.overlays_dir.mkdir(parents=True, exist_ok=True)
        # spread overlays across clusters, worst-IoU first within each
        per_cluster = max(1, args.num_overlays // max(len(by_cluster), 1))
        written = 0
        for cid, recs in by_cluster.items():
            for r in sorted(recs, key=lambda r: r["iou"])[:per_cluster]:
                img = _draw(r["jpg"], r["teacher"], r["student"])
                name = f"{cid}_{r['jpg'].stem}_iou{r['iou']:.2f}.png"
                cv2.imwrite(str(args.overlays_dir / name), img)
                written += 1
        logger.info("Wrote %d overlay PNGs to %s", written, args.overlays_dir)


if __name__ == "__main__":
    main()
