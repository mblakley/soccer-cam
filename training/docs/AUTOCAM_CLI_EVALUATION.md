# AutoCam CLI vs GUI automation — evaluation (2026-08-03)

**Question:** can soccer-cam drive Once AutoCam through its command-line interface instead
of the pywinauto GUI automation in `video_grouper/tray/autocam_automation.py`?

**Answer: yes — proven end-to-end on this hardware, including under the Windows service's
own identity.** One hard blocker (licence activation) and one behavioural gap (the ball
sidecar disappears) must be settled before the switch.

Vendor-side detail — full command reference, engine internals, licence/telemetry
observations, raw logs — is deliberately kept out of this repo and lives at
`F:\archive\OnceAutocam\2026-08-03_autocam_cli_evaluation.md` on the GPU box.

---

## 1. What was proven

Test box: the GPU server (GTX 1060 6GB, 16 GB RAM). AutoCam build dated 2026-06-25 ships
a `AutocamCLI.exe` alongside the GUI; both front-ends sit on the same engine, so the CLI
is a first-class entry point rather than a wrapper around the window.

**Render test.** A 90.05 s / 7680x2160 / 19.85 fps clip (stream-copied from an archived
game on `F:`) rendered to `G:\pipeline_work\test\autocam_cli\`:

| Metric | Value |
|---|---|
| Exit code | 0 |
| Wall clock | 85.9 s (16.9 s of it a one-time engine-selection benchmark) |
| Steady state | ~36.5 ms/frame -> **1.38x realtime** |
| Frames | 1771 processed, 0 dropped |
| Output | H.264 1920x1080 @ 20 fps, 90.047 s, 85.6 MB, 7.97 Mbps, AAC stereo |
| Container | `ftyp/free/mdat/moov` — passes the existing `_validate_autocam_output` gate |

Visual spot-check of an extracted frame: correct broadcast crop following play.

**No desktop required.** The run above executed over PS remoting in session 0. It was then
repeated under `NT AUTHORITY\SYSTEM` (the identity `VideoGrouperService` runs as) via a
scheduled task, with no interactive session, no window station and no pre-existing user
profile: exit code 0, byte-identical output size, GPU inference provider still selected.
**The Windows service can run this step directly — the tray hand-off is no longer needed
for ball tracking.**

**Field marking is headless.** The CLI auto-detects the playing field from its own probe
snapshot during startup. This is precisely the step that is failing in the GUI (below),
and the CLI additionally accepts an explicit field polygon and an explicit snapshot frame,
so soccer-cam can supply the polygon `field_detect` already computes.

**Contract for the pipeline.**
- Exit `0` = success, `1` = processing failure (with a one-line reason on stdout),
  `2` = argument error. Verified all three.
- A machine-parseable progress line lands on stdout about once a second, carrying
  processed/total frame counts, elapsed, ms-per-frame and ETA, and ends with a terminal
  `Succeeded` / `Failed` status line.

Both are strictly better than polling a UI label for the substrings `finished processing`
/ `error` / `framereader_close`.

---

## 2. Why the GUI path is failing right now

Not a soccer-cam bug. On 2026-08-03 the AutoCam GUI launched six times between 13:12 and
13:32 and produced zero renders; the output file for the in-flight game never came into
existence. AutoCam's own log shows an unhandled exception in its settings window while
saving the field-marking snapshot, which kills the UI thread and takes the window with it.
Our driver then sees `Field marking progress: 0/10 points marked` until its 60 s timeout,
clicks controls on a dead window, and eventually times out. The earlier
"Please update AutoCam or contact support" failure is the same class: a front-end refusing
to start work.

Every one of these failure modes is a *window* failure. None of them exists on the CLI
path, which has one process and one exit code.

### Related live bug found while investigating

`video_grouper/tray/autocam_automation.py:19` pins `_AUTOCAM_PROCESS_NAME = "GUI.exe"`,
and `_taskkill_autocam_tree()` kills `GUI.exe` / `autocam.exe`. The production config now
points `[BALL_TRACKING.AUTOCAM_GUI] executable` at the *new* GUI binary, whose image name
is different and appears nowhere in the repo (`git grep` on `origin/main` finds no
occurrence). Consequences: `_find_autocam_gui_pids()` and `_live_autocam_pids()` can never
see the real process, and the cleanup taskkill never kills it. This is worth fixing
regardless of the CLI decision — and it is an argument for the CLI, which needs no
process-name coupling at all.

---

## 3. Gaps and blockers

### 3.1 Licence activation — HARD BLOCKER, untested

The current engine reads a different licence artifact than the one on disk from the 3.0.x
era. Both test runs logged that it was missing, and the output carries a burned-in vendor
watermark in the bottom-right; the archived output from the older licensed GUI run has no
watermark. The CLI documents an `activate <licence-key>` command.

Not tested: no key was available, and activation moves a machine binding. **Until this is
done and verified, CLI output is watermarked.** Also open: whether an activation performed
under one user profile is visible to `LocalSystem`.

### 3.2 The ball-coordinate sidecar disappears — behavioural gap

The old GUI wrote `<output>.mp4.jsonl` next to the output containing per-frame ball
records `{"xy": [x, y], "f": <frame>, "t": <seconds>}`. That file is what
`video_grouper/inference/phase_detector.py::ball_restarts()` reads (glob `**/*.mp4.jsonl`),
feeding `pipeline/steps/phase_detect.py` and `task_processors/phase_game_start.py`.

The current engine writes **no file next to the output**. Its tracking-log option emits
per-frame **camera/viewport centre** coordinates, not ball coordinates, and routes them to
a per-run log under the running user's local app-data. A full run log was checked: no ball
`xy` records anywhere, and the JSON-config surface is exactly the documented flag surface,
so no internal verbose-detection switch is reachable.

Impact: the ball sidecar is one signal among several in phase detection (the
player-on-field curve is the backbone), so phase detection **degrades rather than breaks**.
The size of that degradation was not measured.

Upside: the viewport track is a first-class, documented output now, where previously it had
to be derived. That is the same signal the training-side work already treats as AutoCam's
tracking reference.

### 3.3 Also not tested

- A full-length (90+ min) CLI render, and RAM behaviour over one on a 16 GB box.
- Camera-work quality parity against the old GUI render over a whole game (two frames were
  spot-checked, not a game).
- Supplying our own `field_detect` polygon to the CLI.
- Whether the CLI's per-invocation update check can replace the install mid-pipeline. (A
  plain render invocation did **not** modify the install directory.)

---

## 4. Speed

Indicative, not a controlled A/B (different engine version, 90 s clip vs full game, box
not idle in either case):

| Path | Ratio | 92-min game |
|---|---|---|
| Old GUI engine, from its own completion logs | 0.85–1.02x realtime | 115–120 min |
| CLI, steady state on the same box | 1.38x realtime | ~67 min |

---

## 5. Recommendation

**Adopt the CLI, gated on licence activation.** It removes the interactive-desktop
requirement, the window/dialog/label dependency, the process-name coupling, and the entire
class of failures that burned ~40 minutes of a production batch today. It also exposes
controls the GUI path never had — output bitrate (the 8 Mbps figure turns out to be a
default, not a hardcode), output resolution, inference provider, detector width /
confidence / interval, zoom limits, track smoothing, and an explicit field polygon.

### Code-change sketch (not implemented — for Mark's decision)

1. **New step** `video_grouper/pipeline/steps/autocam_cli.py`
   - `AutocamCliStepConfig(BaseModel)`: `executable`, `execution_provider="auto"`,
     `video_bitrate`, `output_resolution`, `extra_args: list[str]`.
   - `AutocamCliStep(PipelineStep[...])`: `name = "autocam_cli"`,
     `consumes = ("input_path",)`, `produces = ("output_path", "viewport_path")`,
     **`runtime = "service"`** (was `"tray"`), **`resources = ("gpu",)`** (was
     `("autocam_ui",)`).
   - `run()` uses `asyncio.create_subprocess_exec` and consumes stdout line by line:
     parse the `status=` progress line into log/manifest progress; collect the per-frame
     camera-track lines into `<output>.viewport.jsonl`; on exit 0 validate with the
     existing MP4 validator, on non-zero log the single-line error and return `False`.
   - Record the child PID in `state.json` so a service restart can reattach or clean up
     (much simpler than the current window-reattach path).
2. **Move** `_validate_autocam_output` / `_mp4_has_moov_atom` out of
   `video_grouper/tray/autocam_automation.py` into a shared home (e.g.
   `video_grouper/utils/mp4.py`) so the new step can use them without importing pywinauto.
3. **Register** the step in `video_grouper/pipeline/register_steps.py`; add an
   `"autocam_cli"` preset in `video_grouper/pipeline/presets.py`.
4. **Config**: `[BALL_TRACKING.AUTOCAM_CLI] executable = ...` in
   `video_grouper/utils/config.py`, and a `provider == "autocam_cli"` branch in
   `migrate_ball_tracking_to_pipeline()` (`video_grouper/pipeline/config.py`).
5. **Phase detection**: decide between (a) accepting degraded `ball_restarts()` with no
   sidecar, or (b) adding a third source in `phase_detector.py` that reads the new
   `<output>.viewport.jsonl` camera track as a coarse restart proxy. Needs an accuracy
   check on a game that has both.
6. **Delete once migrated**: `video_grouper/tray/autocam_automation.py` (~1200 lines of
   pywinauto/win32gui/file-dialog driving), the `autocam` step's `runtime="tray"`
   hand-off, and the `autocam_ui` resource in `video_grouper/pipeline/resources.py`. The
   tray keeps its other role (OS notifications).
7. **Tests**: replace the pywinauto-mocking tests with subprocess-stub tests asserting the
   argument vector, the progress parse, the exit-code mapping, and the viewport-log write.

### Order of work

1. Activate the licence and re-run the 90 s clip; confirm the watermark is gone under the
   identity that will run the step. **Nothing else matters until this passes.**
2. Full-game CLI render on the archived game, side-by-side against the existing GUI output.
3. Then the code change above.
