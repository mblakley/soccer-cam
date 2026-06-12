"""Export the student to ONNX and verify drop-in parity with the teacher.

The student must match the teacher's exact wire signature so it replaces
the teacher ``.onnx`` with zero changes to
:mod:`video_grouper.inference.field_detector`. ``--check`` asserts the
signature, runs the student through the *unmodified* field_detector path,
measures checkpoint-vs-ONNX deviation, and (given ``--teacher-model``)
compares student vs teacher per-point.

Run on the GPU server / any ml environment.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import cv2
import numpy as np

from training.field_outline import INPUT_H, INPUT_W, NUM_KEYPOINTS
from training.field_outline.dataset import load_samples, polygon_iou

logger = logging.getLogger(__name__)


def _signature(sess) -> dict:
    inp = sess.get_inputs()[0]
    outs = {o.name: list(o.shape) for o in sess.get_outputs()}
    return {
        "in_name": inp.name,
        "in_shape": list(inp.shape),
        "in_type": inp.type,
        "out_shapes": outs,
        "out_types": {o.name: o.type for o in sess.get_outputs()},
    }


def _assert_teacher_signature(sig: dict) -> list[str]:
    """Return a list of signature problems (empty == matches the contract)."""
    problems = []
    if sig["in_name"] != "input":
        problems.append(f"input name {sig['in_name']!r} != 'input'")
    if sig["in_shape"] != [1, 3, INPUT_H, INPUT_W]:
        problems.append(f"input shape {sig['in_shape']} != [1,3,{INPUT_H},{INPUT_W}]")
    if "float16" not in sig["in_type"]:
        problems.append(f"input type {sig['in_type']} not float16")
    if set(sig["out_shapes"]) != {"keypoints", "scores"}:
        problems.append(f"outputs {set(sig['out_shapes'])} != keypoints,scores")
    if sig["out_shapes"].get("keypoints") != [1, NUM_KEYPOINTS, 2]:
        problems.append(f"keypoints shape {sig['out_shapes'].get('keypoints')}")
    if sig["out_shapes"].get("scores") != [1, NUM_KEYPOINTS]:
        problems.append(f"scores shape {sig['out_shapes'].get('scores')}")
    for name, t in sig["out_types"].items():
        if "float16" not in t:
            problems.append(f"output {name} type {t} not float16")
    return problems


def _blob(frame_bgr: np.ndarray) -> np.ndarray:
    """field_detector's exact preprocessing: RGB, 768x384, /255 fp16 NCHW."""
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (INPUT_W, INPUT_H))
    return (resized.astype(np.float16) / 255.0).transpose(2, 0, 1)[np.newaxis]


def run_check(student_onnx: Path, args) -> bool:
    """Signature + drop-in + deviation (+ teacher) checks. Returns ok."""
    import onnxruntime as ort
    import torch

    from training.field_outline.model import FieldOutlineNet
    from video_grouper.inference.field_detector import (
        build_field_polygon,
        create_field_session,
        detect_field_keypoints,
    )

    ok = True
    sess = ort.InferenceSession(str(student_onnx), providers=["CPUExecutionProvider"])
    sig = _signature(sess)
    problems = _assert_teacher_signature(sig)
    print("\n[1] signature:", "OK" if not problems else "FAIL")
    for p in problems:
        print("   -", p)
    ok &= not problems

    if args.teacher_model:
        tsess = ort.InferenceSession(
            str(args.teacher_model), providers=["CPUExecutionProvider"]
        )
        tsig = _signature(tsess)
        same = tsig == sig
        print("[1b] teacher signature identical:", "OK" if same else "FAIL")
        ok &= same

    # sample frames from the dataset for behavioural checks
    samples = load_samples(args.dataset_root) if args.dataset_root else []
    samples = samples[: args.num_parity]
    if not samples:
        print("\n(no dataset frames; skipping behavioural checks)")
        return ok

    # [2] drop-in through the UNMODIFIED field_detector
    fsess = create_field_session(student_onnx, use_gpu=False)
    dropin_ok = True
    for s in samples:
        bgr = cv2.imread(str(s.jpg))
        kpts = detect_field_keypoints(bgr, fsess, score_threshold=0.0)
        poly = build_field_polygon(kpts)
        if len(kpts) != NUM_KEYPOINTS or poly is None:
            dropin_ok = False
    print(
        f"[2] drop-in via field_detector on {len(samples)} frames:",
        "OK" if dropin_ok else "FAIL",
    )
    ok &= dropin_ok

    # [3] checkpoint vs ONNX deviation (768x384 px space)
    ckpt = torch.load(args.checkpoint, map_location="cpu")
    model = FieldOutlineNet(ckpt.get("backbone", "resnet18"), pretrained=False)
    model.load_state_dict(ckpt["model"])
    model.eval()
    scale = np.array([INPUT_W, INPUT_H], dtype=np.float32)
    max_dev = 0.0
    for s in samples:
        blob = _blob(cv2.imread(str(s.jpg)))
        ok_, os_ = sess.run(None, {"input": blob})
        onnx_px = ok_[0].astype(np.float32)
        with torch.no_grad():
            pk, _ = model(torch.from_numpy(blob.astype(np.float32)))
        torch_px = pk[0].numpy() * scale
        max_dev = max(max_dev, float(np.abs(onnx_px - torch_px).max()))
    print(
        f"[3] checkpoint-vs-ONNX max deviation: {max_dev:.3f} px",
        "OK" if max_dev < 2.0 else "WARN",
    )

    # [4] teacher vs student behaviour
    if args.teacher_model:
        tsess_f = create_field_session(args.teacher_model, use_gpu=False)
        deltas, ious, gate_agree = [], [], []
        for s in samples:
            bgr = cv2.imread(str(s.jpg))
            tk = detect_field_keypoints(bgr, tsess_f, score_threshold=0.0)
            sk = detect_field_keypoints(bgr, fsess, score_threshold=0.0)
            tp = np.array([[k[0], k[1]] for k in tk], np.float32)
            sp = np.array([[k[0], k[1]] for k in sk], np.float32)
            deltas.append(np.linalg.norm(tp - sp, axis=1).mean())
            h, w = bgr.shape[:2]
            ious.append(polygon_iou(tp / [w, h], sp / [w, h]))
            t_gate = np.mean([k[2] for k in tk]) >= 0.70
            s_gate = np.mean([k[2] for k in sk]) >= 0.70
            gate_agree.append(t_gate == s_gate)
        print(
            f"[4] vs teacher: mean_pt_delta={np.mean(deltas):.1f}px "
            f"iou={np.mean(ious):.3f} gate_agree={np.mean(gate_agree):.2f}"
        )
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export + verify field-outline student"
    )
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Output .onnx path")
    parser.add_argument("--full-fp16", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--teacher-model", type=Path, default=None)
    parser.add_argument("--dataset-root", type=Path, default=None)
    parser.add_argument("--num-parity", type=int, default=20)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    import torch

    from training.field_outline.model import FieldOutlineNet, export_onnx

    ckpt = torch.load(args.checkpoint, map_location="cpu")
    model = FieldOutlineNet(ckpt.get("backbone", "resnet18"), pretrained=False)
    model.load_state_dict(ckpt["model"])
    model.eval()
    out = export_onnx(model, args.out, full_fp16=args.full_fp16)
    logger.info("Exported %s", out)

    if args.check:
        ok = run_check(out, args)
        print("\n=== parity check:", "PASS ===" if ok else "FAIL ===")
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
