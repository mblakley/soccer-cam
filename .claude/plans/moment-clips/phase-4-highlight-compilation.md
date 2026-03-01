# Phase SC-4 — Highlight Compilation & Delivery

**Status**: NOT STARTED
**Depends on**: SC-3 (clip generation pipeline must be working)
**Goal**: Extend the clip pipeline to compile per-player highlight reels from
individual clips, upload them to Supabase Storage, send NTFY push notifications,
and optionally upload to YouTube.

---

## Approach

When a user requests a highlight reel (via the mobile app), the team-tech-tools
API creates a `highlight_reels` row with status="pending". Soccer-cam's
ClipDiscoveryProcessor (from SC-3) is extended to also check for pending
highlight reels, and queues a HighlightCompilationTask when all clips for the
reel are ready.

### Compilation flow

```
ClipDiscoveryProcessor (extended from SC-3)
  └─> Checks for pending highlight_reels
  └─> Verifies all linked clips have status="ready"
  └─> Queues HighlightCompilationTask
      │
ClipProcessor (reuses SC-3's queue)
  └─> Downloads clips from Supabase Storage (or uses local copies)
  └─> Creates FFmpeg concat file list (chronological order)
  └─> combine_videos() → highlight_reel.mp4
  └─> Upload to Supabase Storage
  └─> Update highlight_reels row (status: "ready", storage_url)
  └─> Send NTFY notification
  └─> Optional: upload to YouTube
```

### NTFY notification

Reuse the existing NtfyAPI to send a push notification when a highlight reel
is ready. Include the player name, game date, and a direct link to the storage
URL. This uses a simple `send_notification()` call — no interactive response
needed.

### YouTube upload (optional)

Reuse the existing YouTubeUploader pattern. If YouTube credentials are configured
and the highlight reel's game session has YouTube upload enabled, upload the reel
to a "Highlights" playlist.

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
    supabase_client.py                # Add download_from_storage() method
```

### Existing (reused)

```
video_grouper/
  utils/ffmpeg_utils.py               # combine_videos() for concat
  api_integrations/ntfy.py            # NtfyAPI.send_notification()
  utils/youtube_upload.py             # YouTubeUploader (optional)
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
  1. Query `highlight_reels` where status="pending"
  2. For each pending reel:
     a. Fetch linked clips via `highlight_reel_clips`
     b. Check all clips have status="ready"
     c. If all ready: collect clip URLs/paths, queue HighlightCompilationTask
     d. If some clips still pending: skip (will retry next poll)
     e. If any clips failed: update highlight_reels status="failed"

### 3. Extend ClipProcessor for highlights

- [ ] Add handling for `HighlightCompilationTask` in `process_item()`:
  1. Execute the task (FFmpeg concat)
  2. Upload reel to Supabase Storage (`highlight-reels/{highlight_id}.mp4`)
  3. Update `highlight_reels` row: status="ready", storage_url
  4. Send NTFY notification (if configured)
  5. Optional: upload to YouTube (if configured)
- [ ] Differentiate between ClipExtractionTask and HighlightCompilationTask
  via `isinstance()` or `task_type` check

### 4. NTFY notification on reel ready

- [ ] Add NTFY notification when a highlight reel is marked "ready"
- [ ] Notification content:
  - Title: "Highlight Reel Ready"
  - Message: "{player_name}'s highlights from {game_date} are ready to watch"
  - Action: "View" button linking to the storage URL
- [ ] Only send if NTFY is configured (check config)
- [ ] Add `send_highlight_notification()` to SupabaseClient or a new helper

### 5. YouTube upload (optional)

- [ ] Add optional YouTube upload after reel compilation
- [ ] Reuse existing `YouTubeUploader` pattern from `upload_processor.py`
- [ ] Upload to a "Highlights" playlist (configurable)
- [ ] Store `youtube_video_id` in highlight_reels table (needs SupabaseClient method)
- [ ] Gated by config: only upload if YouTube credentials are configured
  and highlight upload is enabled

### 6. Supabase Storage download

- [ ] Add `download_from_storage(bucket, path, local_path)` to SupabaseClient
- [ ] Used by HighlightCompilationTask when clips are not available locally
- [ ] Stream download to avoid loading full file into memory

### 7. Supabase Storage bucket for highlights

- [ ] Document bucket creation: `highlight-reels`
- [ ] Path pattern: `{highlight_id}.mp4`
- [ ] Public access for authenticated users

### 8. Unit tests

- [ ] Test HighlightCompilationTask serialize/deserialize
- [ ] Test HighlightCompilationTask.execute() with mocked combine_videos
- [ ] Test highlight discovery (all clips ready → queue task)
- [ ] Test highlight discovery (some clips pending → skip)
- [ ] Test highlight discovery (some clips failed → mark reel failed)
- [ ] Test ClipProcessor handling of HighlightCompilationTask
- [ ] Test NTFY notification sent on reel ready
- [ ] Test YouTube upload gating (disabled when no credentials)

---

## Acceptance Criteria

- Pending highlight reels are discovered by ClipDiscoveryProcessor
- Reel compilation produces a valid MP4 from concatenated clips
- Reels are uploaded to Supabase Storage with accessible URLs
- `highlight_reels` rows are updated with status and storage_url
- NTFY push notification sent when reel is ready
- YouTube upload works when configured (optional)
- Failed compilations are marked as "failed" without blocking the pipeline
- Existing clip generation (SC-3) is not disrupted

---

## Decisions Log

(None yet)
