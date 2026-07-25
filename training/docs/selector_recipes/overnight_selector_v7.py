"""v7 selector retrain (EXP-DIST-43): consolidate near_close AND mid labels ->
ball_labels.jsonl -> build_selector_labels -> kill_test_selector -> export .npz.
Mirrors overnight_selector.py (the canonical v6 recipe) with mid consolidation added,
a held-out guard, and the render step dropped (band_diag does the eval)."""
import subprocess, time, json, shutil, urllib.request
from pathlib import Path

REPO = r"G:\ballresearch\selector\repo"
PY = r"G:\v4bench\wt\.venv\Scripts\python.exe"
B = Path(r"G:\ballresearch\selector")
FL = Path(r"D:\training_data\far_label")
EVAL = [r"G:\ballresearch\distill\cands_spc_hn2.pkl", r"G:\ballresearch\distill\cands_iron_hn2.pkl"]
HELDOUT = ("heat__2026.05.31_vs_Spencerport_gold_2_away", "heat__2026.06.15_vs_Irondequoit_away")
SUFFIXES = ["__near_close", "__mid"]
LOG = B / "overnight_v7.log"
ENV = {"PYTHONPATH": REPO, "PYTHONIOENCODING": "utf-8", "SystemRoot": r"C:\Windows", "PATH": r"C:\Windows\System32;C:\Windows"}


def log(m):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {m}\n")


def ntfy(msg, title):
    try:
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/YOUR_TOPIC", data=msg.encode("utf-8"), headers={"Title": title, "Tags": "robot"}), timeout=30)
    except Exception as e:
        log(f"ntfy {e}")


def run(cmd, name, crit=True):
    log(f"START {name}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, env=ENV)
    out = (r.stdout + r.stderr).strip()
    log(f"{name} rc={r.returncode} :: {out[-1500:]}")
    if r.returncode != 0 and crit:
        ntfy(f"v7 {name} FAILED. See overnight_v7.log.", "v7 FAILED")
        raise SystemExit(1)
    return out


open(LOG, "w").close()
log("=== V7 SELECTOR START ===")
gmap = {}
for base in (r"F:\Heat_2012s", r"F:\Flash_2013s"):
    bp = Path(base)
    if bp.exists():
        for d in bp.iterdir():
            gj = d / "game.json"
            if gj.exists():
                try:
                    gmap[json.loads(gj.read_text(encoding="utf-8", errors="ignore"))["game_id"]] = d
                except Exception:
                    pass

# STEP 0 - consolidate near_close AND mid labels into per-game ball_labels.jsonl
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
    if gid in HELDOUT:
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
        shutil.copy(blp, gd / "ball_labels.jsonl.bak_v7")
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
        p = [float(lab["x"]), float(lab["y"])] if (a == "ball" and lab.get("x") is not None) else None
        add.append({"seg": seg, "f": f, "a": a, "p": p, "src": "human", "set": sd.name, "ts": int(time.time())})
    if add:
        with open(blp, "a", encoding="utf-8") as f:
            for r in add:
                f.write(json.dumps(r) + "\n")
    total_new += len(add)
    log(f"consolidate {gid} ({suf}): +{len(add)} labels")
log(f"consolidated {total_new} new near+mid labels")
ntfy(f"v7: consolidated {total_new} new near+mid labels; retraining.", "v7: consolidated")

# STEP 1 - build_selector_labels for every training fullgame dump
pairs = []
for fg in sorted((B / "fullgame").iterdir()):
    if not fg.is_dir():
        continue
    gid = fg.name
    gd = gmap.get(gid)
    if gd is None or gid in HELDOUT:
        log(f"buildlabels SKIP {gid}")
        continue
    out = B / f"sel_labels_{gid}_v7.json"
    run([PY, "-u", "-m", "training.cli.build_selector_labels", "--fullgame-dir", str(fg), "--game-dir", str(gd), "--out", str(out), "--gold-weight", "20.0"], f"buildlabels_{gid}", crit=False)
    if out.exists():
        pairs.append(f"{fg};{out}")
log(f"built {len(pairs)} selector-label pairs")

# STEP 2 - retrain selector
outp = run([PY, "-u", "-m", "training.cli.kill_test_selector", "--train", *pairs, "--eval", *EVAL, "--save-net", str(B / "selector_v7.pt")], "train_selector")
tail = [l for l in outp.splitlines() if any(k in l for k in ("NEAR", "FAR", "ALL", "ceiling", "argmax", "tracker"))][-16:]
log("EVAL lines:\n" + "\n".join(tail))

# STEP 3 - export
run([PY, "-u", "-m", "training.cli.export_ball_selector", "--pt", str(B / "selector_v7.pt"), "--out", str(B / "selector_v7.npz")], "export")
log("=== V7 SELECTOR DONE ===")
ntfy("v7 selector trained + exported (selector_v7.npz). Held-out eval in overnight_v7.log.", "v7 DONE")
