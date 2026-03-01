# Phase SC-4 — Highlight Compilation & Delivery

**Status**: NOT STARTED
**Depends on**: SC-3 (clip generation pipeline must be working)
**Goal**: Extend the clip pipeline to compile per-player highlight reels from
individual clips, upload them to Supabase Storage and YouTube, and send NTFY
push notifications.

---

## Approach

When a user requests a highlight reel (via the mobile app), the team-tech-tools
API creates a `highlight_reels` row with status="pending". Soccer-cam's
ClipDiscoveryProcessor (from SC-3) is extended to also check for pending
highlight reels via the API, and queues a HighlightCompilationTask when all
clips for the reel are ready.

### Compilation flow

```
ClipDiscoveryProcessor (extended from SC-3)
  └─> Calls API: get pending highlight_reels
  └─> Calls API: get clips linked to each reel
  └─> Verifies all clips have status="ready"
  └─> Queues HighlightCompilationTask
      │
ClipProcessor (reuses SC-3's queue)
  └─> Downloads clips from Supabase Storage (or uses local copies)
  └─> Creates FFmpeg concat file list (chronological order)
  └─> combine_videos() → highlight_reel.mp4
  └─> Upload to Supabase Storage
  └─> Upload to YouTube (direct, reusing existing YouTubeUploader)
  └─> Calls API: update highlight_reels (status: "ready", storage_url)
  └─> Send NTFY notification
```

### YouTube upload

Highlight reels are uploaded directly from soccer-cam to YouTube, following
the same pattern as the existing `UploadProcessor` / `YouTubeUploader`. This
keeps the upload flow consistent — soccer-cam already handles YouTube OAuth
tokens, playlist management, and retry logic.

### NTFY notification

Reuse the existing `NtfyAPI.send_notification()` to push a notification when
a highlight reel is ready. Simple fire-and-forget — no interactive response
needed.

---

## Key Files

### New

```
video_grouper/
  task_processors/
    tasks/clips/
      highlight_compilation_task.py   # BaseTask — concatenate clips into reel
```

### Modified

```
video_grouper/
  task_processors/
    clip_discovery_processor.py       # Add pending highlight_reels discovery
    clip_processor.py                 # Add HighlightCompilationTask handling
    register_tasks.py                 # Register HighlightCompilationTask
  services/
    moment_api_client.py              # Already has highlight methods from SC-1
```

### Existing (reused)

```
video_grouper/
  utils/ffmpeg_utils.py               # combine_videos() for concat
  utils/youtube_upload.py             # YouTubeUploader for direct YouTube upload
  api_integrations/ntfy.py            # NtfyAPI.send_notification()
  task_processors/upload_processor.py  # Pattern reference for YouTube upload flow
```

---

## Tasks

### 1. HighlightCompilationTask

- [ ] Create `video_grouper/task_processors/tasks/clips/highlight_compilation_task.py`
- [ ] Extend `BaseTask` as `@dataclass(unsafe_hash=True)`:
  - `highlight_id: str` — highlight reel UUID
  - `title: str` — reel title
  - `player_name: str | None`
  - `game_session_id: str | None`
  - `clip_storage_urls: list[str]` — ordered list of clip URLs to concatenate
  - `clip_local_paths: list[str]` — local paths if clips exist locally
  - `output_dir: str` — where to save the compiled reel
- [ ] `queue_type` → `QueueType.CLIPS`
- [ ] `task_type` → `"highlight_compilation"`
- [ ] `get_item_path()` → `f"highlight_{highlight_id}.mp4"`
- [ ] `serialize()` / `deserialize()`
- [ ] `execute()`:
  1. Create temp directory for downloaded clips (if not local)
  2. For each clip: use local path if available, else download from Supabase Storage
  3. Create FFmpeg concat file list (text file with `file '/path/to/clip.mp4'` lines)
  4. Call `combine_videos(file_list_path, output_path)`
  5. Clean up temp files
  6. Return True on success
- [ ] Register in `register_tasks.py`

### 2. Extend ClipDiscoveryProcessor for highlights

- [ ] In `discover_work()`, add a second discovery pass:
  1. Call `api_client.get_pending_highlights()` → list of pending reels
  2. For each pending reel:
     a. Call `api_client.get_highlight_clips(highlight_id)` → list of clips
     b. Check all clips have status="ready"
     c. If all ready: collect clip URLs/paths, queue HighlightCompilationTask
     d. If some clips still pending: skip (will retry next poll)
     e. If any clips failed: call `api_client.update_highlight(id, status="failed")`

### 3. Extend ClipProcessor for highlights

- [ ] Add handling for `HighlightCompilationTask` in `process_item()`:
  1. Execute the task (FFmpeg concat)
  2. Upload reel to Supabase Storage (`highlight-reels/{highlight_id}.mp4`)
  3. Upload reel to YouTube (if credentials configured):
     a. Use existing `YouTubeUploader` class
     b. Title: reel title, description: player name + game date
     c. Add to "Highlights" playlist (configurable)
  4. Call `api_client.update_highlight(id, status="ready", storage_url=url)`
  5. Send NTFY notification (if configured)
  6. On failure: call `api_client.update_highlight(id, status="failed")`
- [ ] Differentiate between ClipExtractionTask and HighlightCompilationTask
  via `isinstance()` or `task_type` check

### 4. NTFY notification on reel ready

- [ ] Send NTFY notification when a highlight reel is marked "ready"
- [ ] Notification content:
  - Title: "Highlight Reel Ready"
  - Message: "{player_name}'s highlights are ready to watch"
  - Action: "View" button linking to the storage URL
- [ ] Only send if NTFY is configured (check config)
- [ ] Reuse existing `NtfyAPI.send_notification()` — no interactive flow needed

### 5. YouTube upload for highlight reels

- [ ] Reuse existing `YouTubeUploader` from `utils/youtube_upload.py`
- [ ] Upload highlight reel to YouTube after compilation:
  - Title: highlight reel title
  - Description: player name, game date, team info
  - Playlist: configurable "Highlights" playlist name
- [ ] Gated by config: only upload if YouTube credentials exist and are valid
- [ ] On failure: log warning, don't fail the whole task (reel is still in Storage)
- [ ] Note: youtube_video_id is NOT stored in the API — YouTube upload is
  a best-effort delivery mechanism, not tracked in the database

### 6. Supabase Storage download helper

- [ ] Use `StorageUploader.download_file()` from SC-1 for downloading clips
  that aren't available locally (e.g., clips from a different machine)
- [ ] Prefer local paths when available (faster, no network)

### 7. Supabase Storage bucket for highlights

- [ ] Document bucket creation: `highlight-reels`
- [ ] Path pattern: `{highlight_id}.mp4`
- [ ] Public access for authenticated users

### 8. Unit tests

- [ ] Test HighlightCompilationTask serialize/deserialize
- [ ] Test HighlightCompilationTask.execute() with mocked combine_videos
- [ ] Test highlight discovery (all clips ready → queue task)
- [ ] Test highlight discovery (some clips pending → skip)
- [ ] Test highlight discovery (some clips failed → mark reel failed via API)
- [ ] Test ClipProcessor handling of HighlightCompilationTask (upload + API update)
- [ ] Test NTFY notification sent on reel ready
- [ ] Test YouTube upload gating (disabled when no credentials)
- [ ] Test YouTube upload called when configured

---

## Acceptance Criteria

- Pending highlight reels are discovered via the API
- Reel compilation produces a valid MP4 from concatenated clips
- Reels are uploaded to Supabase Storage with accessible URLs
- Reels are uploaded to YouTube (when configured), same as existing video uploads
- API is called to update highlight reel status and storage_url
- NTFY push notification sent when reel is ready
- Failed compilations are marked as "failed" via API without blocking the pipeline
- Existing clip generation (SC-3) and video pipeline are not disrupted

---

## Decisions Log

(None yet)
