# AutoCam-loses-ball registry (hard-case validation set)

Timestamps where AutoCam visibly loses the ball — the gold-standard held-out validation for "does the
world-model recover the ball where AutoCam drops it." Provided by Mark (his eye for where the viewport
drifts). For each: extract the clip → human-verify the true ball position (the GT) → score whether the
world-model track stays on the ball through the moment AutoCam fails.

**Timestamps are in AutoCam's TRIMMED + CROPPED output** (1920×1080 follow-the-ball), NOT the panorama.
To run our pipeline we use the **source panorama** and map the time:

- **Source pano:** `D:\soccer-cam-storage\2026.05.31-09.42.44\...\combined.mp4` (7680×2160, the input to AutoCam).
- **Trim offset:** `match_info.ini` `start_time_offset = 01:00` → **pano_time = 1:00 + clip_time**.
  So clip 4:45–5:03 → pano **5:45–6:03**, 7:13 → 8:13, 7:43 → 8:43, 9:20 → 10:20, 14:45 → 15:45.
- **Field polygon:** `D:\training_data\v4_fields\heat__2026.05.31_vs_Spencerport_gold_2_away\polygon.json` (human-edited).
- **AutoCam reference:** the 1920×1080 `…05-31-2026.mp4` is AutoCam's output to compare against at these times.

Validation run per clip: extract the pano window → J + motion dump (GPU) → world-model track → render
follow-the-ball → vision-verify the true ball → compare to AutoCam's output at the same moment.

Tags: `far` (ball in far third), `distractor` (locked onto a sideline/adjacent-field ball), `restart`
(throw-in/corner/goal-kick/PK), `handoff` (a different ball played in), `occlusion` (lost behind players).
Status: `pending` (awaiting extraction) → `gt` (true ball positions verified) → `scored`.

| # | Game | Start | End | Tag | Status | Notes |
|---|------|-------|-----|-----|--------|-------|
| 1 | heat__2026.05.31_vs_Spencerport | 4:45 | 5:03 | ? | pending | |
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
