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
| 1 | [API Client](phase-1-api-client.md) | NOT STARTED | HTTP client for team-tech-tools API + Supabase Storage |
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
SC-1 (API client) ──> HTTP calls to team-tech-tools + Supabase Storage uploads
  └─> SC-2 (timestamp matching) ──> pure algorithm, needs RecordingFile data
        └─> SC-3 (clip generation) ──> ClipDiscovery + ClipProcessor pipeline
              └─> SC-4 (highlight compilation) ──> FFmpeg concat + NTFY + YouTube
```

Phases are strictly sequential. SC-1 introduces the API client infrastructure
that all subsequent phases depend on. SC-2 is a pure algorithm with no pipeline
changes. SC-3 adds new processors to the pipeline. SC-4 extends SC-3.

---

## Architecture Decision: API-Only (No Direct DB)

Soccer-cam does NOT connect to the Supabase database directly. All database
operations go through the team-tech-tools REST API. This keeps:
- DB schema knowledge centralized in team-tech-tools
- RLS and auth logic in one place
- Soccer-cam as a pure video processing pipeline

The only direct Supabase interaction is **Storage uploads** — video files are
too large (10-100MB+) to proxy through a Vercel serverless function.

---

## Cross-Repo Dependencies

### team-tech-tools API endpoints needed (must be added first)

These endpoints do NOT exist yet and must be created on the
`feature/moment-tagging` branch before soccer-cam can use them:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/game-sessions?recording_group_dir=X` | Find game session by directory name |
| `GET /api/moment-tags?game_session_id=X&pending_offset=true` | Fetch tags needing offset calculation |
| `PATCH /api/moment-tags/{id}` | Update tag's video_offset_seconds |
| `POST /api/moment-clips` | Create a moment clip row |
| `PATCH /api/moment-clips/{id}` | Update clip status + storage_url |
| `GET /api/highlights?status=pending` | List pending highlight reels |
| `GET /api/highlights/{id}/clips` | Get clips linked to a highlight reel |
| `PATCH /api/highlights/{id}` | Update highlight reel status + storage_url |

### Already exists in team-tech-tools

| Endpoint | Purpose |
|----------|---------|
| `GET /api/game-sessions` | List game sessions (by team_id, status) |
| `GET /api/moment-tags` | List tags (by game_session_id, player_id) |
| `GET /api/moment-clips` | List clips (by game_session_id, player_id, status) |
| `GET /api/highlights` | List highlight reels (by player_id, game_session_id) |

---

## How to Use These Plans

1. **Starting a phase**: Update the status table above and the phase doc header.
2. **Completing a task**: Check the box in the phase doc and note the date.
3. **Decisions**: Record them inline in the phase doc under "Decisions Log".
4. **Scope changes**: Update the phase doc and note the reason.
