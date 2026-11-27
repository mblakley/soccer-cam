---
name: deliver
description: Deliver one soccer-cam product change (feature or fix in video_grouper/, installer, web UI, firmware tooling) end-to-end — fresh worktree off origin/main, plan, implement with tests, local gates, review, docs, PR, green CI, merge, verify where it runs, cleanup. Not for open-ended training research (that follows training/docs conventions).
argument-hint: "<one-line change description or roadmap item>"
---

# /deliver — one change, start to merged and running

Deliver `$ARGUMENTS` through every step, in order. "Done" means **merged to `main` with CI green
and verified where it actually runs** — not "code written". Report evidence at each gate. When a
gate fails, fix the root cause; never skip or weaken it.

## 1. Start clean
- `git fetch origin`, then
  `git worktree add -b <type>/<descriptive-name> ../soccer-cam-<name> origin/main`
  (`type` = feat|fix|chore|docs|refactor; the name describes the change).
- In the worktree: `uv sync`.
- Check for existing work first: open PRs (`gh pr list`), sibling worktrees
  (`git worktree list`), and `training/docs/STATUS.md`. Don't duplicate stranded work — finish it.

## 2. Understand before planning
- Read `training/docs/STATUS.md`; DECISIONS/EXPERIMENTS entries for the area (don't redo a failed
  approach); the code path end to end. Rules for the area load when you open its files
  (`.claude/rules/training.md`, `.claude/rules/firmware.md`).
- Anything checkable gets checked, not assumed (root `CLAUDE.md` rule 7): camera state, server
  state, a third-party tool's behavior.
- Write a one-screen plan: files, tests, how you'll verify where it runs, time estimate.

## 3. Implement with tests
- Unit tests alongside the code (`tests/`, `@pytest.mark.asyncio` on async tests; mock all I/O).
- Video work uses PyAV, not subprocess ffmpeg. Config schema changes go through the legacy-config
  migration path in `video_grouper/utils/config.py` (PR #142 makes this a versioned migration list).
  Web UI uses `chrome.py` + `soccer-cam.css` only.
- TTT calls: a new `/api/internal/*` path must exist in team-tech-tools' `openapi.json`.
- Commit in logical steps on the branch (hooks block commits on `main`).

## 4. Local gates — all must pass before pushing
```bash
uv run ruff check && uv run ruff format --check && uv run mypy video_grouper/
uv run pytest -m "not integration and not e2e"        # ~8 min
uv run pytest tests/web/test_design_system.py          # if web UI changed
uv run pytest -m integration                           # if processors/state/IO changed
```
Paste the pass/fail summary lines. A gate that "was already red" still gets fixed or raised to
Mark — never pushed past.

## 5. Review
- `/code-review` on the branch diff; fix what it confirms.
- `rules-reviewer` agent on `git diff origin/main...HEAD`; fix violations.
- `/security-review` if credentials, auth, network listeners, or the installer changed.

## 6. Docs in the same PR
- `training/docs/STATUS.md` (dated), and DECISIONS/EXPERIMENTS when a choice or test result is
  involved; `docs/` user docs when behavior users see changes. No RE/third-party internals in the
  repo — those go to the F: archive.

## 7. PR → green CI → merge
- `git fetch origin && git rebase origin/main`; push (`--force-with-lease` after a rebase).
- `gh pr create --base main`: what/why, how verified (gate output), how it was verified where it
  runs. End the body with the attribution line from the session reminder.
- `gh pr checks <n> --watch` — must be green. Then `gh pr merge <n> --squash --delete-branch`.

## 8. Verify where it runs, then clean up
- Server code: `git pull` on `DESKTOP-5L867J8` (never edit code there), restart the affected
  service/process, and show it producing output. Installer/tray: test as a customer would.
  Camera: read back on the camera.
- `git worktree remove ../soccer-cam-<name>`; nothing uncommitted or unpushed anywhere you touched.
- Report: change, PR link, gates + CI evidence, where-it-runs evidence, anything deferred and where
  it's recorded.
