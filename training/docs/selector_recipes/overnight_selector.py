"""Overnight PIVOT: selector retrain with the new near GT (uses candidate dumps, not
raw clips — avoids the crop-decode permission bug). Consolidate near labels ->
ball_labels.jsonl (backup+dedup+exact g->(seg,f)) -> build_selector_labels ->
kill_test_selector -> export -> render Spencerport clip 1 with the new selector."""
import subprocess, time, json, shutil, urllib.request
from pathlib import Path
REPO=r"G:\ballresearch\selector\repo"; PY=r"G:\v4bench\wt\.venv\Scripts\python.exe"
B=Path(r"G:\ballresearch\selector"); FL=Path(r"D:\training_data\far_label")
GD_SPC=r"F:\Heat_2012s\2026.05.31 - vs Spencerport gold 2 (away)"
SPC_DUMP=str(B/"fullgame_heldout"/"heat__2026.05.31_vs_Spencerport_gold_2_away")
EVAL=[r"G:\ballresearch\distill\cands_spc_hn2.pkl", r"G:\ballresearch\distill\cands_iron_hn2.pkl"]
LOG=B/"overnight.log"
ENV={"PYTHONPATH":REPO,"PYTHONIOENCODING":"utf-8","SystemRoot":r"C:\Windows","PATH":r"C:\Windows\System32;C:\Windows"}
def log(m):
    with open(LOG,"a",encoding="utf-8") as f: f.write(f"{time.strftime('%H:%M:%S')} {m}\n")
def ntfy(msg,title):
    try: urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/YOUR_TOPIC",data=msg.encode("utf-8"),headers={"Title":title,"Tags":"robot"}),timeout=30)
    except Exception as e: log(f"ntfy {e}")
def run(cmd,name,crit=True):
    log(f"START {name}")
    r=subprocess.run(cmd,capture_output=True,text=True,cwd=REPO,env=ENV)
    out=(r.stdout+r.stderr).strip()
    log(f"{name} rc={r.returncode} :: {out[-1200:]}")
    if r.returncode!=0 and crit:
        ntfy(f"Overnight {name} FAILED. See overnight.log.","Overnight FAILED"); raise SystemExit(1)
    return out
open(LOG,"w").close(); log("=== OVERNIGHT SELECTOR START ===")
# game_id -> F: dir
gmap={}
for base in (r"F:\Heat_2012s", r"F:\Flash_2013s"):
    bp=Path(base)
    if bp.exists():
        for d in bp.iterdir():
            gj=d/"game.json"
            if gj.exists():
                try: gmap[json.loads(gj.read_text(encoding="utf-8",errors="ignore"))["game_id"]]=d
                except Exception: pass
# STEP 0 — consolidate near_close labels
sets=sorted(FL.glob("*__near_close"))
total_new=0
for sd in sets:
    lp=sd/"labels.json"
    if not lp.exists(): continue
    gid=sd.name[:-len("__near_close")]
    gd=gmap.get(gid)
    if gd is None: log(f"consolidate SKIP {gid}: no F: dir"); continue
    gj=json.loads((gd/"game.json").read_text(encoding="utf-8",errors="ignore"))
    segs=gj["segments"]
    def to_segf(g):
        for s in segs:
            o=int(s["global_offset"])
            if o<=g<o+int(s["frames"]): return s["seg"], g-o
        return None,None
    blp=gd/"ball_labels.jsonl"
    existing=set()
    if blp.exists():
        shutil.copy(blp, gd/"ball_labels.jsonl.bak_overnight")
        for ln in blp.read_text(encoding="utf-8",errors="ignore").splitlines():
            if ln.strip():
                try: r=json.loads(ln); existing.add((r["seg"],r["f"]))
                except Exception: pass
    add=[]
    for lab in json.loads(lp.read_text()):
        a=lab.get("action")
        if a=="obscured" or a=="none" or a is None: continue
        g=int(lab["frame_idx"]); seg,f=to_segf(g)
        if seg is None or (seg,f) in existing: continue
        existing.add((seg,f))
        p=[float(lab["x"]),float(lab["y"])] if (a=="ball" and lab.get("x") is not None) else None
        add.append({"seg":seg,"f":f,"a":a,"p":p,"src":"human","set":sd.name,"ts":int(time.time())})
    if add:
        with open(blp,"a",encoding="utf-8") as f:
            for r in add: f.write(json.dumps(r)+"\n")
    total_new+=len(add); log(f"consolidate {gid}: +{len(add)} labels")
log(f"consolidated {total_new} new near labels into ball_labels.jsonl"); ntfy(f"Consolidated {total_new} near labels; retraining selector.","Overnight: consolidated")
# STEP 1 — build_selector_labels for every training fullgame dump
pairs=[]
for fg in sorted((B/"fullgame").iterdir()):
    if not fg.is_dir(): continue
    gid=fg.name; gd=gmap.get(gid)
    if gd is None: log(f"buildlabels SKIP {gid}"); continue
    out=B/f"sel_labels_{gid}_v6.json"
    run([PY,"-u","-m","training.cli.build_selector_labels","--fullgame-dir",str(fg),"--game-dir",str(gd),"--out",str(out),"--gold-weight","20.0"], f"buildlabels_{gid}", crit=False)
    if out.exists(): pairs.append(f"{fg};{out}")
log(f"built {len(pairs)} selector-label pairs")
# STEP 2 — retrain selector
outp=run([PY,"-u","-m","training.cli.kill_test_selector","--train",*pairs,"--eval",*EVAL,"--save-net",str(B/"selector_v6.pt")], "train_selector")
near=[l for l in outp.splitlines() if "NEAR" in l or "near" in l][-6:]
log("NEAR lines:\n"+"\n".join(near))
# STEP 3 — export
run([PY,"-u","-m","training.cli.export_ball_selector","--pt",str(B/"selector_v6.pt"),"--out",str(B/"selector_v6.npz")], "export")
# STEP 4 — render clip 1 with new selector
cp=str(B/"campath"/"spc_clip1_v6.json")
run([PY,"-u","-m","training.cli.plan_camera_path","--net",str(B/"selector_v6.pt"),"--fullgame-dir",SPC_DUMP,"--game-dir",GD_SPC,"--out",cp], "plan")
clip=str(B/"clips"/"spc_clip01_v6_selretrain.mp4")
run([PY,"-u","-m","training.cli.render_camera_path","--camera-path",cp,"--game-dir",GD_SPC,"--start-g","3392","--end-g","9392","--out",clip], "render")
if Path(clip).exists() and Path(clip).stat().st_size>10*1024*1024:
    shutil.copy(clip, r"F:\test\spc_clip01_v6_selretrain.mp4")
    log("STAGED T:\spc_clip01_v6_selretrain.mp4")
    ntfy("DONE: Spencerport clip 1 rendered with the near-retrained SELECTOR -> T:\spc_clip01_v6_selretrain.mp4 (compare vs spc_clip01_v3_reacq). NEAR metric in overnight.log.","Overnight clip ready")
else:
    ntfy("Render produced no clip — see overnight.log","Overnight FAILED")
log("=== OVERNIGHT SELECTOR DONE ===")
