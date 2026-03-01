# Phase SC-1 — API Client

**Status**: NOT STARTED
**Depends on**: team-tech-tools API endpoints (see ROADMAP.md cross-repo deps)
**Goal**: Create an HTTP client for the team-tech-tools REST API and a thin
Supabase Storage upload helper. Soccer-cam does NOT connect to the database
directly — all DB operations go through the API.

---

## Approach

Soccer-cam already depends on `httpx` for HTTP requests. We'll create a
`MomentApiClient` service that wraps authenticated API calls to team-tech-tools.

For Supabase Storage uploads (video files too large for API proxy), we use
direct HTTP uploads to the Supabase Storage REST API via `httpx` — no need for
the full `supabase-py` SDK.

### Authentication

The API client needs a service-level API key or JWT to authenticate with
team-tech-tools. Options:
- **Service role JWT**: Use the Supabase service role key to generate a JWT
  that bypasses RLS (same as the backend uses internally)
- **Dedicated API key**: Create a machine-to-machine API key for soccer-cam

For v1, use the Supabase service role key — it's already used by team-tech-tools
backend and is available as an environment variable.

### Configuration

Add to `config.ini`:

```ini
[MOMENT_TAGGING]
enabled = false
api_base_url = http://localhost:8000
supabase_url = http://localhost:54321
supabase_service_role_key = <key>
```

---

## Key Files

### New

```
video_grouper/
  services/
    moment_api_client.py     # HTTP client for team-tech-tools API
    storage_uploader.py      # Supabase Storage upload via HTTP
```

### Modified

```
video_grouper/
  utils/config.py            # Add MomentTaggingConfig model
pyproject.toml               # No new deps needed (httpx already exists)
```

---

## Tasks

### 1. Config model

- [ ] Add `MomentTaggingConfig` Pydantic model to `utils/config.py`
  - `enabled: bool = False` (feature flag — disabled by default)
  - `api_base_url: str = "http://localhost:8000"` (team-tech-tools API)
  - `supabase_url: str = ""` (for Storage uploads only)
  - `supabase_service_role_key: str = ""`
- [ ] Add `[MOMENT_TAGGING]` section handling to `load_config()` / `save_config()`
- [ ] Add `moment_tagging` field to the root `Config` model
- [ ] Update `config.ini.dist` with example `[MOMENT_TAGGING]` section

### 2. MomentApiClient

- [ ] Create `video_grouper/services/moment_api_client.py`
- [ ] `MomentApiClient` class:
  - `__init__(config: MomentTaggingConfig)` — stores config, creates httpx.AsyncClient
  - `async close()` — close the httpx client
  - Auth: `Authorization: Bearer {service_role_key}` header on all requests

  **Game sessions:**
  - `async get_game_session_by_dir(recording_group_dir: str) -> dict | None`
    → `GET /api/game-sessions?recording_group_dir={dir}`
    → Returns first match or None

  **Moment tags:**
  - `async get_pending_tags(game_session_id: str) -> list[dict]`
    → `GET /api/moment-tags?game_session_id={id}&pending_offset=true`
    → Returns tags where video_offset_seconds IS NULL
  - `async update_tag_offset(tag_id: str, video_offset_seconds: float)`
    → `PATCH /api/moment-tags/{id}` with body `{video_offset_seconds}`

  **Moment clips:**
  - `async create_clip(moment_tag_id: str, game_session_id: str, clip_start: float, clip_end: float, clip_duration: float) -> dict`
    → `POST /api/moment-clips` with body
    → Returns created clip (with id)
  - `async update_clip(clip_id: str, status: str, storage_url: str | None = None, file_path: str | None = None)`
    → `PATCH /api/moment-clips/{id}` with body

  **Highlights:**
  - `async get_pending_highlights() -> list[dict]`
    → `GET /api/highlights?status=pending`
  - `async get_highlight_clips(highlight_id: str) -> list[dict]`
    → `GET /api/highlights/{id}/clips`
  - `async update_highlight(highlight_id: str, status: str, storage_url: str | None = None)`
    → `PATCH /api/highlights/{id}` with body

- [ ] All methods handle HTTP errors gracefully (log + return None/empty list)
- [ ] Timeout: 30s per request
- [ ] Retry: use tenacity for transient failures (already a dependency)

### 3. StorageUploader

- [ ] Create `video_grouper/services/storage_uploader.py`
- [ ] `StorageUploader` class:
  - `__init__(supabase_url: str, service_role_key: str)` — creates httpx.AsyncClient
  - `async close()` — close the client
  - `async upload_file(bucket: str, path: str, local_file_path: str) -> str`
    → `POST {supabase_url}/storage/v1/object/{bucket}/{path}`
    → Headers: `Authorization: Bearer {service_role_key}`, `Content-Type: video/mp4`
    → Streams file (don't load into memory)
    → Returns public URL: `{supabase_url}/storage/v1/object/public/{bucket}/{path}`
  - `async download_file(bucket: str, path: str, local_file_path: str)`
    → `GET {supabase_url}/storage/v1/object/{bucket}/{path}`
    → Streams response to local file
- [ ] Handle large files efficiently (streaming upload/download)

### 4. Unit tests

- [ ] Test config parsing with `[MOMENT_TAGGING]` section
- [ ] Test config defaults when section is missing
- [ ] Test MomentApiClient methods with mocked httpx responses
  - Test successful responses (200/201)
  - Test 404 handling (return None)
  - Test network errors (log + graceful failure)
- [ ] Test StorageUploader upload with mocked httpx
- [ ] Test StorageUploader download with mocked httpx
- [ ] Test auth header is sent on every request

---

## Acceptance Criteria

- Config loads/saves `[MOMENT_TAGGING]` section correctly
- MomentApiClient makes correct HTTP calls with proper auth headers
- StorageUploader can stream upload/download video files
- All methods handle errors gracefully (no crashes on network failures)
- Feature flag (`enabled`) allows disabling entirely
- No new dependencies needed (httpx + tenacity already exist)
- Unit tests pass

---

## Decisions Log

(None yet)
