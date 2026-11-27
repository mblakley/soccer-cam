# TTT integration ports — plan (approved 2026-11-27)

**Goal.** soccer-cam reaches Team Tech Tools only through a handful of small,
generic interfaces ("ports"). TTT is one implementation of each; a Null
implementation (TTT off / not signed in) and a Fake (tests) are the others.
Consumers never construct a TTT client or read `[TTT]` config. TTT stays optional.

Decision record: `training/docs/DECISIONS.md` — "soccer-cam reaches TTT only
through ports" (2026-11-27). Pattern precedent: `video_grouper/schedule/`.

## Ports (≈25 methods, from 79 on `TTTApiClient`)

| Port | Job | Replaces |
|---|---|---|
| `IAccount` | status, begin/complete sign-in, sign-out | auth_server login paths; owned by the device-sign-in PR (other session), wrapped here |
| `IDeviceLink` | register device + cameras (idempotent), heartbeat (reply carries camera-enabled + commands), complete_command | register_service, send_heartbeat, update_camera_status, list_machine_cameras, pending/ack/complete command; hand-typed `camera_id` |
| `IRecordingSink` | recording_found, progress, published, phases | register_recordings, update_recording_step, auto_match_video, game-session phase push |
| `IWorkSource` | pending, claim, details, progress, blocked, complete, fail — kinds: clip, highlight_reel, process_recording, reprocess | 20 methods across clip requests, reels, jobs, reprocess; `MomentApiClient` |
| `IEntitlements` | has, license_model, list_plugins, fetch_plugin | check_entitlement, acquire_model_license, plugin list/download |
| `IPhaseHintSource` | anchors(group_dir) | get_sync_anchors |
| `IScheduleSource` | (exists) | — |

Layout: `video_grouper/integrations/<port>/{base.py, sources/ttt.py, sources/null.py}`,
self-registering. One composition point, `build_integrations(config, storage_path)
-> Integrations`, owns the single shared TTT session and is the only place a TTT
client is constructed. Tests: each port's behaviour tested against the interface
(fakes); each implementation tested on its own (Mark, 2026-11-27).

## Order — one branch (`feat/ttt-integration-ports`), one commit per phase

1. **Composition point + `IEntitlements`.** Smallest real port; removes the
   per-call client in `pipeline/steps/licensed_model.py`. Proves the layout.
2. `IRecordingSink` — reporter recording methods + `phase_ttt_push` (second per-call client).
3. `IWorkSource` + one generic poller replacing `TTTPoller`'s four queues; `MomentApiClient` folds in or goes.
4. `IPhaseHintSource`.
5. `IDeviceLink` + automatic registration on sign-in (fixes hand-typed `camera_id`).
6. `IAccount` over the device-sign-in client (after that PR merges); delete dead
   client methods (31), `ttt_sync_enabled`, pipeline-questions; contract test shrinks with them.

Gates: each phase keeps the unit suite and `tests/contracts/` green; dead code
is deleted only when its port replaces it.

## Not in scope here

Device sign-in itself (TTT `/api/device-auth/*`, PKCE loopback) — other session,
branch `feat/soccer-cam-device-auth`. Config migration v3 is theirs; removing
`[TTT] email/password` is migration v4, after theirs lands.
