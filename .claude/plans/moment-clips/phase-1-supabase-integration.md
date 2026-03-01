# Phase SC-1 — Supabase Integration

**Status**: NOT STARTED
**Depends on**: None (but requires team-tech-tools DB schema to exist)
**Goal**: Add Supabase database connectivity and storage upload capabilities
to soccer-cam. This is new infrastructure — the codebase currently has zero
database dependencies.

---

## Approach

Soccer-cam needs to:
1. **Read** from Supabase PostgreSQL: `game_sessions`, `moment_tags`, `moment_clips`
2. **Write** to Supabase PostgreSQL: update `moment_tags.video_offset_seconds`,
   update `moment_clips.status`/`storage_url`, update `highlight_reels.status`
3. **Upload** to Supabase Storage: clip MP4 files and highlight reel MP4 files

Since this codebase is async throughout, we should use `asyncpg` for database
access (not `psycopg2` which is sync). For storage uploads, use the Supabase
Python client (`supabase-py`).

### Configuration

Add new config section to `config.ini`:

```ini
[SUPABASE]
database_url = postgresql://postgres:postgres@localhost:54322/postgres
storage_url = http://localhost:54321
service_role_key = <service-role-key>
```

The `database_url` points to the same Supabase instance used by team-tech-tools.
The `service_role_key` is needed for storage uploads (bypasses RLS).

---

## Key Files

### New

```
video_grouper/
  services/
    supabase_client.py       # Async DB connection pool + Storage client
```

### Modified

```
video_grouper/
  utils/config.py            # Add SupabaseConfig model
pyproject.toml               # Add asyncpg + supabase dependencies
```

---

## Tasks

### 1. Add dependencies

- [ ] Add `asyncpg` to pyproject.toml dependencies
- [ ] Add `supabase` (supabase-py) to pyproject.toml dependencies
- [ ] Run `uv sync` to update lock file

### 2. Config model

- [ ] Add `SupabaseConfig` Pydantic model to `utils/config.py`
  - `database_url: str` (PostgreSQL connection string)
  - `storage_url: str` (Supabase API URL for storage)
  - `service_role_key: str` (service role key for storage uploads)
  - `enabled: bool = False` (feature flag — disabled by default)
- [ ] Add `[SUPABASE]` section handling to `load_config()` / `save_config()`
- [ ] Add `supabase` field to the root `Config` model
- [ ] Update `config.ini.dist` with example `[SUPABASE]` section

### 3. Supabase client service

- [ ] Create `video_grouper/services/supabase_client.py`
- [ ] `SupabaseClient` class with:
  - `__init__(config: SupabaseConfig)`
  - `async connect()` — create asyncpg connection pool
  - `async close()` — close the pool
  - `async fetch_pending_tags(recording_group_dir: str) -> list[dict]`
    — query `moment_tags` joined with `game_sessions` where
      `video_offset_seconds IS NULL` and `game_sessions.recording_group_dir`
      matches the local group directory
  - `async update_tag_offset(tag_id: str, video_offset: float)`
    — set `moment_tags.video_offset_seconds`
  - `async create_moment_clip(tag_id: str, game_session_id: str, start: float, end: float, duration: float) -> str`
    — insert into `moment_clips` with status "pending", return clip ID
  - `async update_clip_status(clip_id: str, status: str, storage_url: str | None = None, file_path: str | None = None)`
    — update `moment_clips` row
  - `async fetch_pending_highlights() -> list[dict]`
    — query `highlight_reels` where status = "pending"
  - `async fetch_clips_for_highlight(highlight_id: str) -> list[dict]`
    — query `moment_clips` linked via `highlight_reel_clips`
  - `async update_highlight_status(highlight_id: str, status: str, storage_url: str | None = None)`
    — update `highlight_reels` row
  - `async upload_to_storage(bucket: str, path: str, file_path: str) -> str`
    — upload file to Supabase Storage, return public URL
- [ ] All queries must use parameterized statements (no string interpolation)
- [ ] All queries target `coaching_sessions` schema explicitly

### 4. Unit tests

- [ ] Test config parsing with `[SUPABASE]` section
- [ ] Test `SupabaseClient` methods with mocked asyncpg pool
- [ ] Test storage upload with mocked supabase client
- [ ] Test error handling (connection failure, query errors)

---

## Acceptance Criteria

- `uv sync` succeeds with new dependencies
- Config loads/saves `[SUPABASE]` section correctly
- SupabaseClient can be instantiated with config
- All database methods use parameterized queries (no SQL injection)
- Feature flag (`enabled`) allows disabling the integration entirely
- Existing pipeline is not affected when `enabled = False`
- Unit tests pass

---

## Decisions Log

(None yet)
