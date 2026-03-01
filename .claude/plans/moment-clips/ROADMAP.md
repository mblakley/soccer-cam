# Moment Clips — Soccer-Cam Integration

Add moment-tag clip generation and highlight compilation to the soccer-cam
video pipeline. When users tag moments during a game (via the team-tech-tools
mobile app), this pipeline converts those wall-clock timestamps into 30-second
video clips and optionally compiles them into highlight reels.

**Started**: 2026-02-28
**Last updated**: 2026-02-28
**Depends on**: team-tech-tools `feature/moment-tagging` branch (DB schema, API)

---

## Phase Overview

| # | Phase | Status | Target |
|---|-------|--------|--------|
| 1 | [Supabase Integration](phase-1-supabase-integration.md) | NOT STARTED | DB connectivity + storage uploads |
| 2 | [Timestamp Matching](phase-2-timestamp-matching.md) | NOT STARTED | Wall-clock → video offset algorithm |
| 3 | [Clip Generation](phase-3-clip-generation.md) | NOT STARTED | ClipDiscovery + ClipExtraction pipeline |
| 4 | [Highlight Compilation](phase-4-highlight-compilation.md) | NOT STARTED | Highlight reel concat + NTFY notifications |

### Status key

- `NOT STARTED` — no work begun
- `IN PROGRESS` — active development
- `BLOCKED` — waiting on prerequisite or decision
- `DONE` — shipped and verified

---

## Dependency Graph

```
SC-1 (Supabase integration) ──> DB reads/writes + Storage uploads
  └─> SC-2 (timestamp matching) ──> pure algorithm, needs RecordingFile data
        └─> SC-3 (clip generation) ──> ClipDiscovery + ClipProcessor pipeline
              └─> SC-4 (highlight compilation) ──> FFmpeg concat + NTFY + YouTube
```

Phases are strictly sequential. SC-1 introduces the Supabase infrastructure that
all subsequent phases depend on. SC-2 is a pure algorithm with no pipeline changes.
SC-3 adds new processors to the pipeline. SC-4 extends SC-3's discovery processor.

---

## Cross-Repo Context

The team-tech-tools repo (`feature/moment-tagging` branch) has already created:

**Database tables** (Supabase, `coaching_sessions` schema):
- `game_sessions` — links to a soccer-cam recording group via `recording_group_dir`
- `moment_tags` — wall-clock timestamps tagged by users during games
- `moment_clips` — clip metadata (offsets, storage URLs, status)
- `highlight_reels` — compilation requests (status: pending/ready/failed)
- `highlight_reel_clips` — junction table linking reels to clips

**API endpoints** (team-tech-tools backend):
- `GET /api/moment-clips` — list clips (filterable by game, player, status)
- `GET /api/highlights` — list highlight reels
- `POST /api/highlights` — request a new highlight reel (sets status=pending)

Soccer-cam's job: read `moment_tags`, compute video offsets, generate clips,
upload to Supabase Storage, and update `moment_clips` rows with storage URLs.

---

## How to Use These Plans

1. **Starting a phase**: Update the status table above and the phase doc header.
2. **Completing a task**: Check the box in the phase doc and note the date.
3. **Decisions**: Record them inline in the phase doc under "Decisions Log".
4. **Scope changes**: Update the phase doc and note the reason.
