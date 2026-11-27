# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Soccer-cam (video-grouper) is an automated pipeline for downloading, processing, and uploading soccer game videos from IP cameras (Dahua, Reolink). The orchestrator runs as a Windows service or a plain Python process on Linux/Docker. It hosts a FastAPI web app on loopback (port 8765) that serves the dashboard, configuration editor (`/config`), and onboarding wizard (`/setup/*`) — Linux/Docker users get full UI parity with Windows. On Windows, a small PyQt6 tray icon parents AutoCam (when `[BALL_TRACKING].provider = autocam_gui`) and offers Open Dashboard / Restart Service / Exit shortcuts; otherwise the tray is unnecessary.

## Task Execution Rules

**Every task must be verified end-to-end before reporting success.**

1. **Define done before starting.** Before launching anything, state what "success" looks like — a specific output, a process running with expected logs, a file that exists with expected content.

2. **Verify each step before proceeding to the next.** Never chain steps on assumption. After step A completes, confirm its output is correct before starting step B. If A produces files, verify they exist and are valid. If A starts a process, confirm it's running AND producing expected output.

3. **Wait for real evidence.** "Launched" is not "running". "Running" is not "producing results". Check actual output — log lines, result files, GPU utilization, process status. If you can't verify within 60 seconds, set up a check and come back.

4. **Never report success prematurely.** Don't say "training started" until you see the first batch processing. Don't say "file transferred" until you verify the size/checksum matches. Don't say "process is working" from a single log line — wait for sustained progress.

5. **When something fails, fix the root cause.** Don't retry blindly. Understand WHY it failed (permissions? corrupt file? wrong path? process died?) and fix the underlying issue. If the same failure pattern has happened before, fix it permanently.

6. **Track what's actually running.** Maintain awareness of every background process. Know their PIDs, what they're doing, and when they last produced output. A process that hasn't logged in 10 minutes may be dead.

7. **In automated chains, warnings don't exist.** Every guard must hard-fail (non-zero exit,
   refuse to proceed) — a warning printed into a log no chain reads is not a guard. If a tool
   detects that its inputs violate the operator's stated intent, it stops; it does not warn
   and continue. (Added 2026-07-24 after the first-window trap bit a third time, the second
   time sailing past an existing soft warning.)

## Game Naming Convention

All games use this format everywhere — tiles, labels, shards, manifests, configs:

```
{team}__{date}_vs_{opponent}_{location}
```

Examples:
- `flash__2024.06.01_vs_IYSA_home`
- `heat__2024.05.31_vs_Fairport_home`
- `flash__2024.09.27_vs_RNYFC_Black_home`
- `heat__2024.07.20_Clarence_Tournament`

Rules:
- Team prefix: `flash` or `heat` (lowercase)
- Double underscore `__` separates team from date
- Date format: `YYYY.MM.DD` (sortable, unambiguous)
- Single underscore `_` between words
- Location: `home`, `away`, or omitted for tournaments
- No spaces, no parentheses, no dashes in game IDs
- Tournament names: `{team}__{date}_{Tournament_Name}`

The game registry (`F:/training_data/game_registry.json`) is the source of truth for all game IDs and their video source paths.

## File Organization Rules

**Do not create files without considering where they belong.** Follow these rules:

1. **No one-off scripts.** If you need to run something once, use inline Python via `uv run python -c` or a heredoc. Do not create `.py` or `.bat` files for throwaway tasks.
2. **No test/debug files in the codebase.** Files like `test_share_access.py`, `run_heat2.py`, temp scripts — these go in `/tmp` or are run inline, never committed.
3. **Every new file needs a home.** Before creating a file, identify which directory it belongs in based on the project structure below. If it doesn't fit anywhere, reconsider whether it's needed.
4. **Prefer editing existing files** over creating new ones. A new function in an existing module beats a new module.
5. **Clean up after yourself.** If a file was scaffolding or is superseded, delete it in the same session.

### Web UI

The orchestrator's pages are assembled from `video_grouper/web/chrome.py` and
styled by the single shared stylesheet `video_grouper/web/static/soccer-cam.css`
(also served to the annotation tools at `/shared/soccer-cam.css`). Never add a
page-level `<style>` block with its own tokens — see `docs/UI-DESIGN.md`, and
run `uv run pytest tests/web/test_design_system.py` before committing UI work.
Preview every page with `uv run python -m video_grouper.web.preview`.

### Training pipeline, server storage, GPU workers

Moved to `.claude/rules/training.md` (loads automatically when you open a `training/` file).
**Read it before any training, server (`DESKTOP-5L867J8`), or worker task** — it holds the storage
rules (F:/D:/G:, never C: for temp), the pull-local-process-push rule, pipeline-state audit checks,
and the one canonical worker-deploy procedure. Firmware work: `.claude/rules/firmware.md`.

## Build & Development Commands

Package manager: `uv` (not pip)

See `docs/DEVELOPMENT.md` for setup, test timings, and the UI preview command.

```bash
# Everything needed to lint, format and test. No flags required.
uv sync

# Extras, only if you need them: tray = PyQt6, service = pywin32,
# dev = PyInstaller/Cython for building installers.
uv sync --extra tray --extra service

# Run the main application
uv run python run.py

# Run the tray GUI
uv run python -m video_grouper.tray

# Run as Windows service
uv run python -m video_grouper.service
```

### Testing

```bash
# All tests
uv run pytest

# Single test file
uv run pytest tests/test_camera_poller.py

# Single test method
uv run pytest tests/test_dahua_camera.py::TestDahuaCameraAvailability::test_check_availability_failure -v

# Unit tests only (skip integration/e2e)
uv run pytest -m "not integration and not e2e"

# Integration tests only
uv run pytest -m "integration"
```

Test markers: `slow`, `integration`, `e2e`. Async tests use `asyncio_mode = "strict"` — always use `@pytest.mark.asyncio` on async test functions.

### Linting & Formatting

```bash
uv run ruff check --fix    # lint + autofix
uv run ruff format          # format
uv run pre-commit run --all-files  # run all pre-commit hooks
```

Ruff config: double quotes, spaces, target Python 3.13.

## Architecture

### Processing Pipeline

The app runs 6 task processors orchestrated by `VideoGrouperApp` (video_grouper/video_grouper_app.py):

```
CameraPoller → DownloadProcessor → VideoProcessor → UploadProcessor
                                        ↑
StateAuditor ──────────────────────── NtfyProcessor
```

**CameraPoller** polls the camera for new .dav files, groups them by timestamp, and queues downloads.
**DownloadProcessor** downloads files from the camera to local storage.
**VideoProcessor** runs FFmpeg to combine .dav→MP4 and trim videos.
**NtfyProcessor** sends push notifications asking the user to identify game start/end times.
**UploadProcessor** uploads finished videos to YouTube.
**StateAuditor** scans all video directories and queues work based on state.json status.

### Two-Tier Processor Pattern

All processors extend one of two base classes in `video_grouper/task_processors/`:

- **PollingProcessor** (`base_polling_processor.py`): Discovery-based. Runs `discover_work()` on an interval. Used by CameraPoller and StateAuditor.
- **QueueProcessor** (`base_queue_processor.py`): Work-processing. Maintains an async queue with JSON-persisted state, deduplication via `get_item_key()`, and max 3 retries. Used by Download, Video, Upload, and Ntfy processors.

### Task System

Tasks are registered in `task_processors/task_registry.py` for serialization/deserialization. Key task types: `CombineTask`, `TrimTask`, `YoutubeUploadTask`, `GameStartTask`, `GameEndTask`, `TeamInfoTask`. All extend `BaseTask`.

### State Management

Each video group directory gets a `state.json` tracking file states: `pending → downloading → downloaded → combined → trimmed → complete`. State is read/written with `FileLock` for concurrency safety (`video_grouper/models/directory_state.py`).

### Configuration

Pydantic models in `video_grouper/utils/config.py`. Loaded from INI file (default: `shared_data/config.ini`). Sections: CAMERA, STORAGE, APP, PROCESSING, LOGGING, TEAMSNAP, PLAYMETRICS, YOUTUBE, NTFY, AUTOCAM.

### External Integrations

- **TeamSnap** (`api_integrations/teamsnap.py`): OAuth 2.0, fetches game schedules
- **PlayMetrics** (`api_integrations/playmetrics.py`): pure-HTTP client (Firebase auth → PlayMetrics REST API); no browser
- **TTT** (`api_integrations/ttt_api.py`, `ttt_reporter.py`, `moment_api_client.py`): team-tech-tools worker API under `/api/internal/*`. A path added here must exist in TTT's `openapi.json`
- **NTFY** (`api_integrations/ntfy.py`): Push notifications with image attachments
- **YouTube** (`utils/youtube_upload.py`): Google OAuth 2.0 upload
- **Dahua Camera** (`cameras/dahua.py`): HTTP/Digest auth for file listing and download

### Test Conventions

- Global fixtures in `tests/conftest.py`: `mock_ffmpeg`, `mock_file_system`, `mock_httpx`, `temp_storage`, `mock_config`, `cleanup_asyncio_tasks`
- Unit tests mock all I/O; integration tests use temp directories with real processor instances
- Always run from project root for proper path resolution

## Verification

Always verify changes before committing (the same gates CI runs in `.github/workflows/lint.yml`):
1. `uv run ruff check` — must pass with no errors
2. `uv run ruff format --check` — must show no reformatting needed
3. `uv run mypy video_grouper/` — must pass
4. `uv run pytest -m "not integration and not e2e"` — all unit tests must pass (~8 min)
5. UI work: `uv run pytest tests/web/test_design_system.py`

Use `/verify` to run all checks at once.

## Documentation Rules

The git repo is the source of truth for all project knowledge. Claude memory is for user preferences only — project state goes in these files under `training/docs/`:

| File | Purpose | When to update |
|------|---------|----------------|
| `STATUS.md` | What's happening right now | Start AND end of every work session |
| `DECISIONS.md` | Why we chose X over Y | When choosing between approaches |
| `EXPERIMENTS.md` | What we tried, what happened | After every experiment or significant test |
| `GAMES.md` | Per-game quality observations | When learning something about a specific game |
| `ROADMAP.md` | High-level plan with checkboxes | When tasks complete (add dates) |

### Rules

- **Read STATUS.md before starting work** — don't duplicate effort or miss context from prior sessions.
- **Never store project state in Claude memory** — if it's about the project (not user preferences), it goes in a doc file and gets committed.
- **Failed experiments are required reading** — before trying something, check EXPERIMENTS.md. Don't repeat failures.
- **Decisions are permanent** — never delete a DECISIONS.md entry. If reversed, add a new entry explaining why.
- **Dates on everything** — every STATUS.md update, every experiment, every decision gets a date.
- **Commit docs with code** — documentation updates should be committed alongside the code changes they describe.
- **Don't document chores** — data cleanup, disk space management, file copies, directory renames are operational work. Only document things that affect the project's direction: architectural choices, experiment results, dataset changes, model improvements.

## Workflow — delivering a change

**Use `/deliver <change>` for every product feature or fix** (`.claude/skills/deliver/SKILL.md`):
fresh worktree off `origin/main` → plan → implement with tests → local gates (Verification above) →
`/code-review` + `rules-reviewer` agent → docs in the same PR → PR → green CI → merge → deploy/pull
where it runs → remove worktree. Research/experiments follow `training/docs/` conventions instead,
on their own branch, and are never deleted.

**Definition of Done:** merged to `main` via PR with CI green; local gates passed before push; tests
added with the change; review findings fixed; docs updated in the same PR (STATUS/DECISIONS/
EXPERIMENTS as applicable); verified where it actually runs (server `git pull` + process producing
output, camera, or installer as a customer would); nothing uncommitted or unpushed left behind.

Hooks enforce part of this (`.claude/hooks/`): `guard-git.sh` blocks commits on `main` or in the
primary checkout and pushes to `main`; `format-edited-file.sh` runs ruff on each edited `.py`;
`stranded-work.sh` stops a turn from ending with uncommitted or unpushed work.

- Start sessions in Plan mode (Shift+Tab twice) for non-trivial changes
- Worktrees: `git fetch origin && git worktree add -b <type>/<name> ../soccer-cam-<name> origin/main`
- `/verify-app` adds integration tests and a startup check on top of `/verify`
