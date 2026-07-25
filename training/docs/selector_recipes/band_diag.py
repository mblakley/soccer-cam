"""Per-band (near/mid/far) v5-vs-v6 diagnostic: GT-in-viewport, detector ceiling, selection on-ball.
Shows whether v6 regressed and which depth band is weakest (mid = unlabeled). env: GAME_DIR, DUMP_DIR, SEL_NPZ."""
import bisect, json, os, sys
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

GD = os.environ["GAME_DIR"]
gj = json.loads(open(os.path.join(GD, "game.json"), encoding="utf-8", errors="ignore").read())
offs = dd.seg_offsets(gj["segments"]); poly = np.asarray(gj["field_polygon"], float)
geom = build_field_geometry(poly); s0 = gj["segments"][0]; SW, SH = int(s0["w"]), int(s0["h"])
hb, _ = dd.load_human_labels(os.path.join(GD, "ball_labels.jsonl"), offs)
ef, cands, _ = load_fullgame_candidates(Path(os.environ["DUMP_DIR"])); N = len(ef)
yn = float(np.mean(poly[0:5, 1])); yf = float(np.mean(poly[5:10, 1])); sp = max(yn - yf, 1e-6)


def depth01(y):  # 0 = far endline, 1 = near touchline
    return float(np.clip((y - yf) / sp, 0, 1))


def band(y):
    d = depth01(y)
    return "far" if d < 0.34 else ("mid" if d < 0.67 else "near")


def nearest_ef(g):
    i = bisect.bisect_left(ef, g); best = None
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(ef) and abs(ef[j] - g) <= 2:
            if best is None or abs(ef[j] - g) < abs(ef[best] - g): best = j
    return best


SEL = load_selector(os.environ.get("SEL_NPZ", r"G:\ballresearch\selector\models\selector_v5.npz"))
frames = [[Candidate(x=x, y=y, score=s) for (x, y, s, _z) in cands[g]] for g in ef]
gaps = [1] + [ef[i] - ef[i - 1] for i in range(1, N)]
feats = [x[:, SEL.keep] for x in build_features(frames, geom, ef=ef)]
pk, mk = pack_frames(feats, top_k=max(24, max((len(f) for f in feats), default=1))); pr = predict_probs(SEL, pk, mk)
pri = [-np.log(np.maximum(pr[i, :len(fr)], 1e-6)) if fr else np.zeros(0) for i, fr in enumerate(frames)]
ms = [float(-np.log(max(float(pr[i, -1]), 1e-6))) for i in range(N)]
base = dict(alpha=0., static_w=2., motion_w=0., phys_sigma_px=5., bridge_w=2., oob_w=2., reacq_cap_max_m=60.)
sel = rerank(frames, geom, frame_gaps=gaps, priors=pri, miss_costs=ms, config=replace(RerankConfig(), **base))
tr = upsample_track(kalman_smooth(sel, geom), ef, ef[0], ef[-1] + 1, max_gap=24)
d01 = [None if p is None else depth01(p[1]) for p in tr]
pl = plan_camera(tr, src_w=SW, src_h=SH, depth01=d01, config=PlannerConfig())
cmd = {ef[0] + i: p for i, p in enumerate(pl)}
selimg = {ef[i]: (float(xy[0]), float(xy[1])) for i, xy in sel.items()}

R = 100
B = {b: dict(n=0, gtin=0, ceil=0, selon=0) for b in ("near", "mid", "far")}
for g, (bx, by) in hb.items():
    b = band(by); B[b]["n"] += 1
    if g in cmd:
        cx, cy, hf = cmd[g]; hw = 7680 * (hf / 180.) / 2.; hh = hw * 1080 / 1920
        if abs(bx - cx) <= hw and abs(by - cy) <= hh: B[b]["gtin"] += 1
    j = nearest_ef(g)
    if j is not None:
        gf = ef[j]
        if min((np.hypot(x - bx, y - by) for (x, y, s, _z) in cands[gf]), default=1e9) <= R: B[b]["ceil"] += 1
        if gf in selimg and np.hypot(selimg[gf][0] - bx, selimg[gf][1] - by) <= R: B[b]["selon"] += 1

tot = sum(B[b]["n"] for b in B); tin = sum(B[b]["gtin"] for b in B)
print(f"MODEL {os.path.basename(os.environ.get('SEL_NPZ','v5'))}  GAME {os.path.basename(GD)[:34]}  GT={tot}  GT-in-view(all)={tin/max(tot,1):.3f}")
for b in ("near", "mid", "far"):
    d = B[b]; n = max(d["n"], 1)
    print(f"   {b:4s} n={d['n']:4d} | GT-in-view {d['gtin']/n:.3f} | ceiling@{R} {d['ceil']/n:.3f} | selected-on-ball {d['selon']/n:.3f}")
