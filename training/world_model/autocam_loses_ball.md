# AutoCam-loses-ball registry (hard-case validation set)

Timestamps where AutoCam visibly loses the ball — the gold-standard held-out validation for "does the
world-model recover the ball where AutoCam drops it." Provided by Mark (his eye for where the viewport
drifts). For each: extract the clip → human-verify the true ball position (the GT) → score whether the
world-model track stays on the ball through the moment AutoCam fails.

**Timestamps are in the TRIMMED + PROCESSED video** (the soccer-cam pipeline output), not raw segments.
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
