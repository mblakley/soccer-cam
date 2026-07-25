import json,sys
from dataclasses import replace
from pathlib import Path
import numpy as np
sys.path.insert(0, r"G:\ballresearch\selector\repo")
from training.cli.build_selector_labels import load_fullgame_candidates
from training.data_prep import distill_dataset as dd
from video_grouper.inference.ball_selector import build_features, load_selector, pack_frames, predict_probs
from video_grouper.inference.ball_tracker import Candidate, RerankConfig, kalman_smooth, rerank
from video_grouper.inference.camera_planner import PlannerConfig, plan_camera, upsample_track
from video_grouper.inference.world_geometry import build_field_geometry
GD=Path(r"F:\Heat_2012s\2026.05.31 - vs Spencerport gold 2 (away)")
DUMP=r"G:\ballresearch\selector\fullgame_heldout\spc_stride4"
gj=json.loads((GD/"game.json").read_text(encoding="utf-8",errors="ignore"))
offs=dd.seg_offsets(gj["segments"]); poly=np.asarray(gj["field_polygon"],float)
geom=build_field_geometry(poly); s0=gj["segments"][0]; sw,sh=int(s0["w"]),int(s0["h"])
hb,_=dd.load_human_labels(GD/"ball_labels.jsonl",offs); ac={}
for ln in (GD/"autocam_viewport.jsonl").read_text(encoding="utf-8",errors="ignore").splitlines():
    if ln.strip():
        r=json.loads(ln); sg=offs.get(r.get("seg"))
        if sg is not None: ac[sg+int(r["f"])]=(float(r["x"]),float(r["y"]))
LO,HI=3392,9392
ef_all,cands,_=load_fullgame_candidates(Path(DUMP))
ef=[g for g in ef_all if LO<=g<HI]
frames=[[Candidate(x=x,y=y,score=s) for (x,y,s,_z) in cands[g]] for g in ef]
gaps=[1]+[ef[i]-ef[i-1] for i in range(1,len(ef))]
CH=replace(RerankConfig(),alpha=0.,static_w=2.,motion_w=0.,phys_sigma_px=5.,bridge_w=2.,oob_w=2.,reacq_cap_max_m=60.)
def d01(t):
    yn=float(np.mean(poly[0:5,1])); yf=float(np.mean(poly[5:10,1])); sp=max(yn-yf,1e-6)
    return [None if p is None else float(np.clip((p[1]-yf)/sp,0,1)) for p in t]
def evalnet(npz):
    sel=load_selector(npz)
    feats=[x[:,sel.keep] for x in build_features(frames,geom,ef=ef)]
    pk,mk=pack_frames(feats); pr=predict_probs(sel,pk,mk)
    pri=[-np.log(np.maximum(pr[i,:len(fr)],1e-6)) if fr else np.zeros(0) for i,fr in enumerate(frames)]
    ms=[float(-np.log(max(float(pr[i,-1]),1e-6))) for i in range(len(frames))]
    s=rerank(frames,geom,frame_gaps=gaps,priors=pri,miss_costs=ms,config=CH)
    tr=upsample_track(kalman_smooth(s,geom),ef,LO,HI)
    pl=plan_camera(tr,src_w=sw,src_h=sh,depth01=d01(tr),config=PlannerConfig())
    cmd={LO+i:p for i,p in enumerate(pl)}
    iv=wr=n=0
    for g,(bx,by) in hb.items():
        if g not in cmd: continue
        cx,cy,hf=cmd[g]; hw=7680*(hf/180.)/2.; hh=hw*1080/1920; n+=1
        if abs(bx-cx)<=hw and abs(by-cy)<=hh: iv+=1; continue
        if g in ac and abs(bx-cx)>abs(bx-ac[g][0]): wr+=1
    return iv/n,wr,n
for nm,npz in [("v5",r"G:\ballresearch\selector\models\selector_v5.npz"),("v6",r"G:\ballresearch\selector\selector_v6.npz")]:
    iv,wr,n=evalnet(npz)
    print(f"{nm} + stride-4 + cap60:  ball-in-view {iv:.3f}  GT-wrong {wr} (of {n})")
