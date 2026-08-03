# The `autocam_cli` pipeline step

**Status:** implemented, not yet run in production. Added 2026-08-03.

Ball-tracking render driven by Once AutoCam's **command-line front-end**
(`AutocamCLI.exe`) instead of its desktop GUI. Same vendor engine; a plain
subprocess instead of window automation.

---

## Why

The existing `autocam` step is an adapter around ~1200 lines of pywinauto in
`video_grouper/tray/autocam_automation.py`: launch the app, find its main
window by title prefix, find its process by image name, walk two file dialogs,
click "Auto mark", poll a status label for magic substrings, and wait for a
success string. Because that needs an interactive desktop, the step declares
`runtime = "tray"` and the Windows service must hand the game off to the tray to
get it done.

Every production failure of that path so far has been a **window** failure:

* the vendor renamed its process image, so the liveness check could never see a
  perfectly healthy render and declared the run dead ~6 s in (fixed in
  `e59dcae` by hard-coding the new name — a coupling that has to be maintained
  on every vendor release);
* a settings window raised in its own UI thread while saving a field-marking
  snapshot, killing the window our driver was clicking on;
* a front-end that refuses to start work at all shows up as a 60 s timeout
  clicking a dead control.

None of those failure modes exists on a process with one exit code. The CLI also
runs headless under the service's own identity, which removes the
service→tray handoff for ball tracking entirely, and it does its field marking
automatically — the step that was failing in the GUI.

The prior evaluation (usage, measured timings, exit-code contract, the headless
`LocalSystem` run) is on branch `feat/operator-w1-scoreboard` at
`training/docs/AUTOCAM_CLI_EVALUATION.md`.

---

## Design

### Step contract

| | `autocam` (GUI) | `autocam_cli` (this) |
|---|---|---|
| `runtime` | `tray` | **`service`** |
| `resources` | `("autocam_ui",)` | **`("gpu",)`** |
| `requires` | — | — (stdlib subprocess) |
| `consumes` | `input_path` | `input_path` |
| `produces` | `output_path` | `output_path` |

`resources = ("gpu",)` because the engine runs inference on a GPU provider, so
it should serialize against `ball_detect` / `field_detect` / `phase_detect`
rather than against a window that no longer exists. `autocam_ui` stays as-is for
the GUI step.

`requires = ()` means the step is available in every bundle, including the tray
bundle. It still won't *run* there — `runtime = "service"` makes the tray hand it
back — but nothing greys out or fails to import.

### Coexistence with the GUI step

This change is **additive**. `autocam` is untouched: same module, same
registration, same `autocam_ui` resource, same preset. Selection is config:

* preset `autocam_cli` (`apply_preset("autocam_cli")`), alongside `autocam`;
* or a hand-written `[PIPELINE.<id>]` with `type = autocam_cli`;
* or legacy `[BALL_TRACKING] provider = autocam_cli` + `[BALL_TRACKING.AUTOCAM_CLI]`,
  which `migrate_ball_tracking_to_pipeline()` lifts into a `[PIPELINE]` section
  on first load (exactly like `autocam_gui` does today).

Nothing in the GUI path is deprecated or deleted here. Retiring
`autocam_automation.py` and the `autocam_ui` resource is a separate change to
make once the CLI path has run a full season's worth of games.

### Execution model

```
_spawn(argv)                     asyncio.create_subprocess_exec, stderr folded into stdout
  └─ readline loop               one reader, one buffer
       ├─ status=... line        parsed into key=value tokens -> throttled INFO log
       └─ anything else          DEBUG, and kept in a 20-line tail for the failure message
  └─ EOF -> await proc.wait()    exit code is the authority, not any log substring
       ├─ rc != 0                log the last error-ish line + last status, discard partial, return False
       └─ rc == 0                validate the MP4, discard partial + return False if it isn't complete
```

**Progress.** A line like

```
status=Running processed=522 dropped=0 total=547 elapsed=00:01:04 msPerFrame=117.25 eta=00:00:02
```

arrives about once a second, ending on `status=Succeeded` (or `status=Failed`).
Durations are `HH:MM:SS`; a failing run reports `total=N/A` / `eta=N/A`, and
`processed` need not reach `total` on success — so the parser treats the line as
free-form `key=value` tokens, computes a percentage only when `total` is a
usable number, and passes every other field through to the log rather than
matching an allowlist. A 90-minute game is ~5400 of those lines, so the step
logs the first, then at most one per 60 s, then the terminal line.

**Timeout.** There is **no total-runtime budget**. A full game legitimately runs
for over an hour, and fixed deadlines are precisely what killed healthy renders
on the GUI path. Instead there is a *stall guard*:
`progress_timeout_seconds` (default 900) is the maximum time with **no stdout at
all**; hitting it kills the process tree, discards the partial and fails the
step. Startup includes a one-time engine-selection benchmark before the first
progress line, so the default is deliberately generous.

**Cancellation.** The runner's `cancel_request.json` check happens between
steps, so a mid-render cancel arrives as `asyncio.CancelledError` (service
shutdown, task cancellation). The step catches it, kills the child, discards the
partial and re-raises. Nothing in that handler awaits — awaiting inside a
cancelled task can re-raise before the cleanup finishes. This is a real gain
over the GUI path, where an orphaned render could keep a GPU busy for an hour.

**Killing.** By **PID** (`taskkill /F /T /PID`), never by image name. The
image-name coupling is exactly what broke when the vendor renamed its binary.

**Failure mapping.**

| Condition | Result |
|---|---|
| `executable` unset / not found (and not on PATH) | `False`, no process spawned |
| `input_path` missing | `False`, no process spawned |
| spawn raises `OSError` | `False` |
| exit `2` (argument error) | `False`, last error line logged |
| exit `1` (processing failure) | `False`, last error line logged, partial discarded |
| exit `0`, output fails MP4 validation | `False`, partial discarded |
| exit `0`, output validates | `True` |
| no stdout for `progress_timeout_seconds` | kill tree, discard partial, `False` |

A step returning `False` is a failed step to the runner, which records it in the
manifest and lets normal retry/resume apply. Every failure path deletes the
output **only** if it exists and fails validation, so a genuinely complete file
is never thrown away.

The reported cause is the `[cli] Error: …` line. It is picked explicitly rather
than by scanning for the last line mentioning "error", because a failing run
ends with telemetry lines whose event name contains `RuntimeError` — a naive
scan surfaces one of those and buries the real reason.

### Output container

The MP4 moov-atom gate is **not** universal: AutoCam's GUI defaults its
destination to `.mkv`, and `get_ball_tracking_io_paths()` already takes
`output_ext="mp4" | "mkv"`, so a Matroska output is a reachable configuration.
An MP4-only gate would reject a perfectly good `.mkv` render as "no moov atom".
The validator therefore branches on the output suffix:

| Suffix | Check |
|---|---|
| `.mp4` / `.m4v` / `.mov` | size floor + moov-atom walk (finalization proven) |
| `.mkv` / `.mka` / `.webm` | size floor + EBML header ID (**finalization not** proven) |
| anything else | size floor only |

The Matroska branch is deliberately weaker and says so in its reason string:
the EBML header ID is fixed by the spec so we can prove the file *is* Matroska,
but proving the writer finalized it needs an EBML parser this doesn't have.
Today's production path passes `output_ext="mp4"`, so this is a guard against a
one-line config change, not a live gap.

### Output validation is shared, not copied

`_mp4_has_moov_atom` / `_validate_autocam_output` moved out of
`video_grouper/tray/autocam_automation.py` (which imports `pywinauto` and
`win32gui` at module top and is therefore unimportable from the service) into
**`video_grouper/utils/mp4.py`** as `mp4_has_moov_atom` /
`validate_video_output`. The tray module re-exports them under their historical
private names, so its callers and tests are unaffected. Both paths now run the
same gate — a clean exit code is not evidence the container was finalized, which
is the 2026-06-01 header-only-MP4 failure mode.

A regression test imports the step in a clean interpreter with `pywinauto` and
`win32gui` poisoned, so this property can't silently rot.

---

## Config surface

`[PIPELINE.<step_id>]` with `type = autocam_cli` (or the legacy
`[BALL_TRACKING.AUTOCAM_CLI]`, which migrates to the same thing):

| Key | Default | Meaning |
|---|---|---|
| `executable` | *(unset — required)* | Full path to `AutocamCLI.exe`, or a name on PATH |
| `mode` | `basic` | Processing mode passed as `--mode` |
| `overlay_icon` | *(unset)* | Logo overlay image (`--overlay-icon`) |
| `overlay_scale` | *(unset)* | Overlay size (`--overlay-scale`) |
| `execution_provider` | *(unset)* | `auto` \| `cpu` \| `cuda` \| `dml` \| `coreml` |
| `video_bitrate` | *(unset)* | Passed verbatim to `--video-bitrate` |
| `output_resolution` | *(unset)* | `WIDTHxHEIGHT`, or `720p` / `1080p` / `1440p` / `2160p` / `4k` |
| `extra_args` | *(empty)* | Any other documented flag; appended last |
| `progress_timeout_seconds` | `900` | Stall guard (see above) |

Design rule: **every optional flag is omitted unless configured**, so the
default argv is exactly

```
AutocamCLI.exe --mode basic --input <in> --output <out>
```

— the invocation that was proven end-to-end. No unmeasured flag sits on the
default path.

`execution_provider` is whitelisted and normalized at config-validation time so
a typo fails when the step is constructed, with a readable message, rather than
as an opaque argument-error exit. `video_bitrate` / `output_resolution` are
*not* interpreted — this step doesn't guess the vendor's accepted spelling; a
bad value surfaces as the CLI's own argument error. `extra_args` accepts a
comma-separated string or a Python-literal list from INI (the same convention as
`[NODE] capabilities`) and is always appended last, so it adds options rather
than replacing ones the step already passes. Use the literal-list form for any
value that itself contains a comma:
`extra_args = ['--field-polygon', '10,20;30,40']`.

### Overlay / branding

`--overlay-icon` is the CLI spelling of the GUI's "Add Logo" option — ordinary
product configuration. Point `overlay_icon` at the team's own logo (or a fully
transparent PNG) and set `overlay_scale` to taste. Both are unset by default:
out of the box the step applies no overlay configuration at all and takes the
vendor's default behaviour.

---

## The ball sidecar: decision

**The GUI path wrote a `<output>.mp4.jsonl` per-frame ball-position sidecar. The
CLI does not. This step does not attempt to replace it.**

What consumed it: `video_grouper/inference/phase_detector.py::ball_restarts()`
globs `**/*.mp4.jsonl` for `{t, xy}` records and turns them into ball-restart
events. That feeds `pipeline/steps/phase_detect.py` and
`task_processors/phase_game_start.py`.

**Impact:** phase detection **degrades, it does not break**. `ball_restarts()`
never raises on absence — it returns no events — and the ball signal is one of
three fused inputs. The player-on-field curve is the detector's backbone and the
whistle signal is unaffected. The size of the degradation has **not** been
measured.

**Why not synthesize a replacement.** The CLI's tracking-log option emits
per-frame **camera/viewport centre** coordinates, not ball coordinates, and
writes them to a per-run log rather than next to the output. Feeding a viewport
track into a function whose thresholds were tuned on ball positions would
silently change phase-detection behaviour in an *unmeasured* way. An
unmeasured absence is better than an unmeasured substitution: the absence is
visible (no events), the substitution is not.

**Not silent.** The step logs, on every successful run:

```
autocam_cli: no ball-position sidecar is produced by the CLI;
phase detection runs on the player-curve + whistle signals only
```

**Follow-up (not in this change).** On a game that has both a GUI render (with
sidecar) and a CLI render, measure fused phase boundaries with and without the
ball signal. If the loss is material, add the viewport track as an explicit
third source in `phase_detector.py` — as its own signal with its own thresholds,
not disguised as ball positions. The strategic answer is different anyway: the
`homegrown` preset produces a real ball trajectory (`trajectory_path`) from our
own detector, which is strictly better than either.

---

## Verified directly against the binary (3.1.1)

Checked by running `AutocamCLI.exe help` and two throwaway invocations, and by
reading a real run log — not taken on trust:

* Exit codes: unknown flag → **2**; unopenable input → **1**. Both confirmed.
* The failure line is `[cli] Error: <cause>`, followed by a `status=Failed …`
  line and then telemetry lines.
* The progress line shape quoted above, including `HH:MM:SS` durations,
  `total=N/A` on failure, and `processed < total` on success.
* `--execution-provider` accepts `auto, cpu, cuda, dml, coreml` — **`coreml`
  included**, which an earlier version of this step's whitelist rejected.
* `--mode` is `basic` or `stitch`; stitch takes
  `--input-left`/`--input-right`/`--stitch-maps` instead of `--input`, so this
  step supports `basic` only and refuses `stitch` at config time.
* `--overlay-scale` is documented `0` to `1`, relative to output width — now
  range-checked at config time.
* `--output-resolution` accepts `WIDTHxHEIGHT` or `720p/1080p/1440p/2160p/4k`.
* `--field-polygon x1,y1;x2,y2;...` and `--enable-tracking-log` both exist.
* The CLI checks for updates on every invocation, so a render invocation is not
  guaranteed to be offline-clean.

## Open items

Still **not** verified:

1. **A full-length render.** The end-to-end proof was a 90 s clip. Untested: a
   90+ minute game, and RAM behaviour over one.
2. **Camera-work parity** against a GUI render over a whole game (two frames
   were spot-checked, not a game).
3. **Supplying our own `field_detect` polygon** via `--field-polygon`. The flag
   exists and `field_detect` already produces the polygon, but the CLI's own
   auto-detect is headless and works, so this step does not pass one. Add it
   only with a measured reason.
4. **The accepted spelling for `--video-bitrate` values** (help says only "set
   the target video bitrate") — hence the pass-through.
5. **`--enable-tracking-log` output location and format** (see the sidecar
   section). Reachable today via `extra_args`; nothing consumes it.
6. **Matroska finalization** — see the output-container table.
