"""v8 selector retrain (EXP-OP-35): METRIC-CONSISTENT ray-geometry selector.
Mirrors overnight_selector_v7.py (near_close+mid consolidation -> ball_labels.jsonl
-> build_selector_labels -> kill_test_selector -> export .npz) with TWO changes:

1. **--world-model ray end-to-end** (EXP-OP-34's design constraint, measured):
   the teacher tracker, the snap/jump meters, the selector FEATURES and the
   internal replay all run on the ray-ground geometry (correct meters) instead
   of the planar homography (bows -28% near / +42% far, EXP-OP-32). Piecemeal
   metric swaps regress — features AND tracker together, at BOTH training and
   eval time. Eval replays of this net MUST pass
   ``--world-model ray --feature-world-model ray`` to operator_ladder run-a.
2. **Upper 90 joins the held-out guard** (EXP-OP-37): never in training or
   tuning, under either spelling (upper90 / Upper_90). kill_test_selector's
   HELD_OUT_TOKENS hard-fail is the backstop.

GATE (EXP-DIST-65 trap): depth-feature changes can exploit position-biased
distill labels — the decision gate is HUMAN-GT CONTAINMENT on the 6-game F-OP
replay (v8+ray vs v7 incumbent, both under dcB flags), NEVER the selector's
internal eval numbers below (which are in ray meters and not comparable to
v7's planar printouts anyway)."""

import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

REPO = r"G:\ballresearch\selector\repo"
PY = r"G:\v4bench\wt\.venv\Scripts\python.exe"
B = Path(r"G:\ballresearch\selector")
FL = Path(r"D:\training_data\far_label")
EVAL = [
    r"G:\ballresearch\distill\cands_spc_hn2.pkl",
    r"G:\ballresearch\distill\cands_iron_hn2.pkl",
]
HELDOUT = (
    "heat__2026.05.31_vs_Spencerport_gold_2_away",
    "heat__2026.06.15_vs_Irondequoit_away",
)
# EXP-OP-37: the new held-out GT game — NEVER in training or tuning.
HELDOUT_TOKENS = ("upper90", "upper_90")
SUFFIXES = ["__near_close", "__mid"]
LOG = B / "overnight_v8.log"
ENV = {
    "PYTHONPATH": REPO,
    "PYTHONIOENCODING": "utf-8",
    "SystemRoot": r"C:\Windows",
    "PATH": r"C:\Windows\System32;C:\Windows",
}


def is_held_out(name: str) -> bool:
    low = name.lower()
    return name in HELDOUT or any(t in low for t in HELDOUT_TOKENS)


def log(m):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {m}\n")


def ntfy(msg, title):
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "https://ntfy.sh/YOUR_TOPIC",
                data=msg.encode("utf-8"),
                headers={"Title": title, "Tags": "robot"},
            ),
            timeout=30,
        )
    except Exception as e:
        log(f"ntfy {e}")


def run(cmd, name, crit=True):
    log(f"START {name}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, env=ENV)
    out = (r.stdout + r.stderr).strip()
    log(f"{name} rc={r.returncode} :: {out[-1500:]}")
    if r.returncode != 0 and crit:
        ntfy(f"v8 {name} FAILED. See overnight_v8.log.", "v8 FAILED")
        raise SystemExit(1)
    return out


open(LOG, "w").close()
log("=== V8 SELECTOR START (ray metric, EXP-OP-35) ===")
gmap = {}
for base in (r"F:\Heat_2012s", r"F:\Flash_2013s"):
    bp = Path(base)
    if bp.exists():
        for d in bp.iterdir():
            gj = d / "game.json"
            if gj.exists():
                try:
                    gmap[
                        json.loads(gj.read_text(encoding="utf-8", errors="ignore"))[
                            "game_id"
                        ]
                    ] = d
                except Exception:
                    pass

# The eval .pkl dumps carry no source dims; kill_test needs them for the ray
# geometry. Read them from the eval games' own game.json (self-verifying —
# hard-fail on absence or mismatch rather than assume).
dims = set()
for gid in HELDOUT:
    gd = gmap.get(gid)
    if gd is None:
        log(f"FATAL: eval game {gid} has no F: dir — cannot derive --src-dims")
        raise SystemExit(1)
    seg0 = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))[
        "segments"
    ][0]
    dims.add((int(seg0["w"]), int(seg0["h"])))
if len(dims) != 1:
    log(f"FATAL: eval games disagree on source dims {sorted(dims)}")
    raise SystemExit(1)
SRC_W, SRC_H = dims.pop()
log(f"eval src dims {SRC_W}x{SRC_H}")

# STEP 0 - consolidate near_close AND mid labels into per-game ball_labels.jsonl
# (idempotent — dedupes against existing rows; unchanged from v7)
sets = []
for suf in SUFFIXES:
    sets += sorted(FL.glob("*" + suf))
total_new = 0
for sd in sets:
    lp = sd / "labels.json"
    if not lp.exists():
        continue
    suf = next((s for s in SUFFIXES if sd.name.endswith(s)), None)
    gid = sd.name[: -len(suf)]
    if is_held_out(gid):
        log(f"consolidate SKIP {gid}: HELD-OUT (eval only)")
        continue
    gd = gmap.get(gid)
    if gd is None:
        log(f"consolidate SKIP {gid}: no F: dir")
        continue
    gj = json.loads((gd / "game.json").read_text(encoding="utf-8", errors="ignore"))
    segs = gj["segments"]

    def to_segf(g, segs=segs):
        for s in segs:
            o = int(s["global_offset"])
            if o <= g < o + int(s["frames"]):
                return s["seg"], g - o
        return None, None

    blp = gd / "ball_labels.jsonl"
    existing = set()
    if blp.exists():
        shutil.copy(blp, gd / "ball_labels.jsonl.bak_v8")
        for ln in blp.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ln.strip():
                try:
                    r = json.loads(ln)
                    existing.add((r["seg"], r["f"]))
                except Exception:
                    pass
    add = []
    for lab in json.loads(lp.read_text()):
        a = lab.get("action")
        if a == "obscured" or a == "none" or a is None:
            continue
        g = int(lab["frame_idx"])
        seg, f = to_segf(g)
        if seg is None or (seg, f) in existing:
            continue
        existing.add((seg, f))
        p = (
            [float(lab["x"]), float(lab["y"])]
            if (a == "ball" and lab.get("x") is not None)
            else None
        )
        add.append(
            {
                "seg": seg,
                "f": f,
                "a": a,
                "p": p,
                "src": "human",
                "set": sd.name,
                "ts": int(time.time()),
            }
        )
    if add:
        with open(blp, "a", encoding="utf-8") as f:
            for r in add:
                f.write(json.dumps(r) + "\n")
    total_new += len(add)
    log(f"consolidate {gid} ({suf}): +{len(add)} labels")
log(f"consolidated {total_new} new near+mid labels")
ntfy(f"v8: consolidated {total_new} new near+mid labels; retraining on ray.", "v8")

# STEP 1 - build_selector_labels (RAY metric) for every training fullgame dump
pairs = []
for fg in sorted((B / "fullgame").iterdir()):
    if not fg.is_dir():
        continue
    gid = fg.name
    gd = gmap.get(gid)
    if gd is None or is_held_out(gid):
        log(f"buildlabels SKIP {gid}")
        continue
    out = B / f"sel_labels_{gid}_v8.json"
    run(
        [
            PY,
            "-u",
            "-m",
            "training.cli.build_selector_labels",
            "--fullgame-dir",
            str(fg),
            "--game-dir",
            str(gd),
            "--out",
            str(out),
            "--gold-weight",
            "20.0",
            "--world-model",
            "ray",
        ],
        f"buildlabels_{gid}",
        crit=False,
    )
    if out.exists():
        pairs.append(f"{fg};{out}")
log(f"built {len(pairs)} selector-label pairs (ray)")

# STEP 2 - retrain selector on ray features (kill_test verifies the labels'
# world_model matches and hard-fails on any held-out leak)
outp = run(
    [
        PY,
        "-u",
        "-m",
        "training.cli.kill_test_selector",
        "--train",
        *pairs,
        "--eval",
        *EVAL,
        "--world-model",
        "ray",
        "--src-dims",
        str(SRC_W),
        str(SRC_H),
        "--save-net",
        str(B / "selector_v8.pt"),
    ],
    "train_selector",
)
tail = [
    line
    for line in outp.splitlines()
    if any(k in line for k in ("NEAR", "FAR", "ALL", "ceiling", "argmax", "tracker"))
][-16:]
log(
    "EVAL lines (RAY meters — diagnostics only, gate is F-OP human-GT):\n"
    + "\n".join(tail)
)

# STEP 3 - export
run(
    [
        PY,
        "-u",
        "-m",
        "training.cli.export_ball_selector",
        "--pt",
        str(B / "selector_v8.pt"),
        "--out",
        str(B / "selector_v8.npz"),
    ],
    "export",
)
log("=== V8 SELECTOR DONE ===")
ntfy(
    "v8 selector (ray metric) trained + exported. Gate = F-OP 6-game containment.",
    "v8 DONE",
)
