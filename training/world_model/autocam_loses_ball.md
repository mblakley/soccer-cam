# AutoCam-loses-ball registry (hard-case validation set)

Timestamps where AutoCam visibly loses the ball — the gold-standard held-out validation for "does the
world-model recover the ball where AutoCam drops it." Provided by Mark (his eye for where the viewport
drifts). For each: extract the clip → human-verify the true ball position (the GT) → score whether the
world-model track stays on the ball through the moment AutoCam fails.

Timestamps map **directly** to the trimmed panorama (the `-raw.mp4` is already trimmed to the game —
68:59, matching AutoCam's 68:55 output — so **no offset**):

- **Source pano (trimmed, 7680×2160):** `D:\soccer-cam-storage\2026.05.31-09.42.44\2026.05.31 - BU14 -
  Guzzetta vs Spencerport gold 2 (Total Sports Experience)\bu14---guzzetta-spencerport-gold-2-total-sports-experience-05-31-2026-raw.mp4`
  (19.481 fps). Frame = clip_time × 19.481. Clip 1 (4:45–5:03) = frames **5552–5902**.
- **Field polygon:** `D:\training_data\v4_fields\heat__2026.05.31_vs_Spencerport_gold_2_away\polygon.json`.
- **AutoCam reference:** the 1920×1080 `…05-31-2026.mp4` — AutoCam's follow-the-ball output to compare at the same times.

Validation per clip: dump J+motion on the frame window (GPU) → world-model track → render follow-the-ball
→ vision-verify the true ball → compare to AutoCam's output at the same moment.

Tags: `far` (ball in far third), `distractor` (locked onto a sideline/adjacent-field ball), `restart`
(throw-in/corner/goal-kick/PK), `handoff` (a different ball played in), `occlusion` (lost behind players).
Status: `pending` (awaiting extraction) → `gt` (true ball positions verified) → `scored`.

| # | Game | Start | End | Tag | Status | Notes |
|---|------|-------|-----|-----|--------|-------|
| 1 | heat__2026.05.31_vs_Spencerport | 4:45 | 5:03 | far+occlusion | **scored — WIN** | 73 ball + 15 occluded. **AutoCam 0.01 vs WM-fused 0.52 @R400** (motion-protected suppression; was 0.41 before the fix). AutoCam ~2852px off (lost, centered). Ceiling: ball in fused cands 0.93@400 → tracker still has headroom (MHT). |
| 2 | heat__2026.05.31_vs_Spencerport | 7:13 | 7:30 | ? | pending | |
| 3 | heat__2026.05.31_vs_Spencerport | 7:43 | 8:05 | ? | pending | |
| 4 | heat__2026.05.31_vs_Spencerport | 9:20 | 9:35 | ? | pending | |
| 5 | heat__2026.05.31_vs_Spencerport | 14:45 | 14:57 | ? | pending | |

## Games still to source (held-out Reolink, field polygon on disk → turnkey)
- `heat__2026.05.07 vs Pittsford`
- `heat__2026.06.07 vs Lakefront`
- `flash__2026.05.09 vs Cleveland Force` (Flash, for variety)
(Spencerport 05.31 polygon also on disk — these clips are immediately runnable once I locate the
trimmed+processed video on the box.)
