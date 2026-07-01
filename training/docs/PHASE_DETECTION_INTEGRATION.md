# Plan: Game-phase detection → production pipeline + TTT verify-loop

**Status:** draft for review (2026-06-18). Cross-repo: **soccer-cam** (OSS) + **team-tech-tools** (closed).
**Goal:** make the offline game-phase detector a first-class production feature that (1) can replace the
NTFY "did the game start?" walk, (2) publishes phase timestamps to TTT for display, and (3) is
human-verified + correctable through a TTT-triggered, NTFY-screenshot review loop.

---

## Decisions (locked with Mark, 2026-06-18)

1. **soccer-cam config switch** for game-start: `phase_detection` | `ntfy`, **default `phase_detection`**.
2. **Reviewer = camera manager via NTFY.** soccer-cam sends transition screenshots to its own NTFY
   topic; the camera manager taps Correct / Not-Correct. No per-user NTFY routing.
3. **Edit in TTT → soccer-cam re-runs.** Corrected timestamps are edited in TTT; TTT raises a
   reprocess request; soccer-cam claims it and re-runs (re-trim if KO/END moved).
4. **TTT consumer = metadata + display only** (for now). Phases are stored on the game session and
   shown in the UI; no clip/highlight automation consumes them yet.
5. **Validation = the verify-loop itself.** There is no clean held-out set (the detector was tuned on
   ~all 18 Reolink GT games); 6/15 Irondequoit is the only genuinely-new game (not yet on YouTube).
   The production Correct/Not-Correct rate is the real, continuous generalization metric.
6. **Gating = free.** Auto-phase-detection + the TTT-side display/verify are free (adoption lead-magnet;
   the detector runs locally in soccer-cam regardless). No `check_entitlement` gate on this feature.
7. **Dahua cameras use NTFY.** `phase_detection` mode auto-falls-back to the NTFY game-start walk when
   `video_format` is Dahua (no usable whistle audio, ~45%). Only Reolink (whistle-capable) gets
   auto-phase-detection.
8. **Keep the current coarse trim buffer.** The detected KO sets `start_time_offset` using the existing
   4-min backup (`GAME_START_BACKUP_SECONDS`), same as the NTFY walk — safe (never cuts into the start);
   do NOT tighten it.
9. **Branch off `feat/game-phase-detection`** (NOT `main`) for this work — it consumes the detector that
   lives there; the detector branch merges to `main` in sequence later.
10. **Detector core lives with the DELIVERED code**, not training. The signals + fusion move to
    `video_grouper/inference/phase_detector.py` (next to `field_detector.py`).
    `training/data_prep/phase_detect.py` becomes a thin CLI importing the core for registry-loop GT
    scoring. (Box-scratch verification co-locates the core flat in `G:\ballresearch\` via an import shim.)

---

## UPDATE 2026-06-18 — untrimmed-KO validation + pivot

Ran the detector via `detect_phases` on the **untrimmed combined** video (production regime) with the
real field polygon, vs human GT, for 6 reolink games. Result: **it does NOT reliably find the game
start** — the warm-up before kickoff confuses KO. 05.10 −2s and 05.07 −1s were clean, but 03.21 was
**+527s with `ok=True`** (a confident, sanity-gate-passing 9-min-late KO → would trim 9 min into the
game), and 05.09 / 05.27 / 05.28 were hundreds of seconds off (mostly `ok=False` → NTFY fallback).
The detector was tuned on **trimmed** (game-only) videos where KO ≈ 0:00; the untrimmed regime is
unvalidated and currently unsafe for an on-by-default trim.

**Decision (Mark, 2026-06-18): fix untrimmed KO** rather than fall back to post-trim-only enrichment.
So decision 1 is **suspended**: game-start stays NTFY *until the detector clears a bar on untrimmed
combined videos* (KO reliably within tolerance AND no confident-wrong `ok=True` far misses like 03.21).
The `phase_detect` pipeline step (post-trim, validated 53/63) and the TTT push/display/verify (S2–S4,
T1–T2) are **deferred** behind this — no point wiring the verify-loop until KO on the production
video is trustworthy. T1 (TTT schema) is committed and stands. This is now a **detector-research
task**: make KO/HT/2H/END robust on the untrimmed combined video (warm-up before KO, post-game after
END), and tighten the sanity gate so confident-wrong far-KOs are rejected.

---

## What already exists (reuse-first — do NOT reinvent)

| Need | Existing primitive | Where |
|---|---|---|
| Run detection as a pipeline stage | `PipelineStep` model + `field_detect`/`ball_detect` steps | `video_grouper/pipeline/` |
| Field polygon (detector dependency) | `field_detect` step already produces it | `pipeline/steps/field_detect.py` |
| Person model / whistle / ball deps | already in the inference bundle | pipeline `requires` |
| Set the trim point | `match_info.start_time_offset` (what the NTFY walk writes today) | `tasks/ntfy/game_start_task.py` |
| Push phases to TTT DB | `update_game_session(session_id, ...)` keyed by `recording_group_dir` | `api_integrations/ttt_api.py` |
| Screenshot + Correct/Not-Correct via NTFY | `create_pipeline_question(image_url, actions)` + `get_pipeline_question` (poll) | `ttt_api.py` + `services/ttt_question_service.py` |
| Edit → reprocess (cross-NAT) | `get_reprocess_queue` / `claim_reprocess_request` / `update_reprocess_status` + `reprocess_request_processor` | `ttt_api.py`, `task_processors/` |
| Recording handle join | `recording_group_dir` / `camera_id` / `get_camera_recording` | throughout (never youtube id) |
| Free vs premium gating | `check_entitlement` / capabilities | `ttt_api.py`, `plugins/entitlement_check.py` |
| NTFY question UX pattern | `GameStartTask` (BaseNtfyTask: screenshot + actions + response handling) | `tasks/ntfy/` |

**The detector itself** (`training/data_prep/phase_detect.py`) is currently a training-side script.
It must be refactored into an importable, side-effect-free module the production step can call.

---

## Architecture / flow

```
combine done (combined.mp4)
        │
        ▼
[A] phase_detect pipeline step  ── needs: combined.mp4 + field_polygon (+ ball sidecar if present)
        │  emits KO/HT/2H/END + confidence(ok) to the manifest & game.json (source="phase_fused")
        │
        ├─ game_start_method == phase_detection AND fit ok?
        │     yes → set match_info.start_time_offset = KO − buffer   (replaces the NTFY walk)
        │     no/rejected/Dahua-low-conf → fall back to the NTFY game-start walk (never silent-fail)
        │
        ▼
[B] push to TTT (inline)  ── update_game_session(recording_group_dir, phase fields, source)
        │   (only if camera manager has a TTT account; community installs keep phases local)
        ▼
[C] verify loop (TTT-triggered, on demand)
        TTT "Verify phases" button → reprocess/command in TTT queue
        → soccer-cam polls, renders a screenshot at each transition,
          create_pipeline_question(image_url=shot, actions=[Correct, Not-Correct]) per transition
        → camera manager taps on NTFY; soccer-cam polls get_pipeline_question
        → Correct → mark phase verified;  Not-Correct → flag for edit
        ▼
[D] edit + reprocess
        camera manager edits the wrong timestamp in TTT → "request reprocess"
        → reprocess request (carries corrected phases) → soccer-cam claims it
        → re-trim if KO/END changed, re-store phases (source="human"), re-push to TTT
```

---

## soccer-cam work (branch `feat/phase-detection-game-start`, off `feat/game-phase-detection`; one commit per phase)

- **S0 — Move the detector into the delivered code.** Extract the signals + fusion from
  `training/data_prep/phase_detect.py` into **`video_grouper/inference/phase_detector.py`** (next to
  `field_detector.py`), exposing `compute_signals(...)`, `fuse_phases(signals) -> {times, ok, used,
  meta}` (trimmed-time, no `voff`), and `detect_phases(combined_video, field_polygon,
  ball_sidecar=None) -> result` — no `sys.argv`/printing/global side effects. `phase_detect.py` becomes
  a thin training CLI importing the core (with a flat-import shim so box-scratch verification still
  runs). Unit-test `fuse_phases` on 2–3 cached signal fixtures (06.08, 06.06-S, 05.28). **Gate:** the
  box `--predict --gt-only` + `phase_eval --human-only` scorecard must stay **reolink 53/63** exactly.
- **S1 — `phase_detect` pipeline step + config switch.** New registered step at
  `video_grouper/pipeline/steps/phase_detect.py` (`type = phase_detect`, `consumes` combined video +
  field polygon, `produces` a phases artifact) that calls `video_grouper.inference.phase_detector`. Add
  `[PROCESSING] game_start_method = phase_detection|ntfy` (default `phase_detection`). On `ok` fit, set
  `match_info.start_time_offset` from KO using the **existing coarse 4-min backup**
  (`GAME_START_BACKUP_SECONDS`, unchanged — decision 8). **Fall back to the existing GameStartTask walk**
  when: `ntfy` mode, the camera is **Dahua** (`video_format`, decision 7), or the fit is rejected /
  no-whistle-low-confidence — so behavior is never worse than today. Persist all four phases to
  game.json (`game_state`, source `phase_fused`).
- **S2 — Push phases to TTT.** After the step, if a TTT session exists for this `recording_group_dir`,
  `update_game_session(...)` with the four phase offsets + source + confidence. Inline (no scheduled
  loop). No-account installs skip the push (phases stay local).
- **S3 — Verify-loop producer.** On a TTT "verify phases" command (polled from the reprocess/command
  queue), render a frame at each transition from the local combined video and
  `create_pipeline_question` per transition (screenshot + Correct/Not-Correct). Poll responses; write
  Correct/Not-Correct back to TTT (`update_game_session` verified flags). Reuse `ttt_question_service`
  + the `GameStartTask` screenshot helper.
- **S4 — Edit→reprocess consumer.** Extend `reprocess_request_processor` to accept a
  `phase_correction` request carrying corrected timestamps: overwrite game.json phases (source
  `human`), re-trim if KO/END moved, re-push to TTT, `update_reprocess_status` through the lifecycle.

## team-tech-tools work (branch off `development`; one commit per phase)

- **T1 — Schema + write endpoint.** Add phase-timestamp columns (kickoff / halftime / second_half /
  end + per-phase `source` + `verified` + `confidence`) to the **game_sessions** model (per-recording;
  the multi-camera junction already exists). Extend the `update_game_session` route to accept them.
  Migration + seed updates in the same PR (per repo convention). TestClient + RLS integration tests.
- **T2 — Display + verify trigger + edit.** Show the four phases on the game/session view; a "Verify
  phases" action that enqueues the verify command for soccer-cam; inline timestamp editing that, on
  "request reprocess", creates a reprocess request carrying the corrected phases. Player/coach views
  read-only; camera-manager view editable. (Display-only consumer — no clip wiring yet.)
- **T3 — Gating: FREE (decided).** No `check_entitlement` gate on phase display/verify; it's an
  adoption lead-magnet and the detector runs locally regardless. Nothing to build here beyond not
  gating — folded into T1/T2.

---

## Validation

- **Held-out:** run the (already-auto-detected) phases on **6/15 Irondequoit** and either (a) Mark
  uploads it to YouTube → verify in the existing editor, or (b) I vision-verify rendered transition
  frames. Report KO/HT/2H/END error vs Mark's verdict. Acknowledge n=1 is weak.
- **Continuous:** the production Correct/Not-Correct rate across all new games is the real
  generalization metric — log it; it tells us when to retune. This is the strongest argument for
  building the verify-loop.
- **Honesty bar:** current 84% within-10s is **in-sample Reolink**; **Dahua ≈ 45%** (no whistle).
  Ship with the NTFY fallback so a wrong/low-confidence detection never produces a bad trim silently.

---

## Open questions / to verify in Phase 0

1. **Detector deps in the production bundle.** Confirm the `phase_detect` step's `requires`
   (onnxruntime/cv2/av + person model) and that `field_detect` runs before it in the default pipeline.
   Whistle-only is the floor; KO leans on field-polygon + (optional) ball sidecar.
2. **Ball sidecar timing.** The detector's KO/2H lean on AutoCam ball restarts. At the phase-detect
   stage the AutoCam sidecar may not exist yet → confirm ordering, or run KO on whistle+player-curve
   only when the sidecar is absent (degraded but workable).
3. **Verify trigger transport.** Reuse the reprocess queue, the generic `pending-commands` channel, or
   a new `pipeline_questions` batch? Confirm which is cleanest for "verify N transitions at once."
4. **Multi-camera display** — phases per recording; confirm the session view aggregates / picks a
   canonical camera.

(Resolved 2026-06-18: gating = free; Dahua = NTFY fallback; trim buffer = keep coarse 4-min;
branch off `feat/game-phase-detection`.)

## Sequencing

S0 → S1 (usable: auto game-start for Reolink with NTFY fallback, no TTT needed) → T1 → S2 (phases in
TTT) → T2 + S3 (verify loop) → S4 (+ T2 reprocess). Gating is "don't gate" (free), folded into T1/T2.
S0–S1 deliver standalone value before any TTT work. Cross-repo worktrees: one short-lived branch per
repo — soccer-cam off **`feat/game-phase-detection`** (merges to `main` in sequence later), TTT off
`development`.
