# Phase SC-3 — Clip Generation Pipeline

**Status**: NOT STARTED
**Depends on**: SC-1 (Supabase client), SC-2 (timestamp matcher)
**Goal**: Add a ClipDiscoveryProcessor and ClipProcessor to the pipeline that
discover pending moment tags, compute video offsets, extract 30-second clips
with FFmpeg, upload them to Supabase Storage, and update the database.

---

## Approach

Two new processors integrate into the existing pipeline:

```
Existing pipeline:
  CameraPoller → DownloadProcessor → VideoProcessor → UploadProcessor
                                          │
New additions (runs after VideoProcessor produces trimmed video):
  ClipDiscoveryProcessor (PollingProcessor, every 60s)
    └─> Finds game_sessions matching local recording group directories
    └─> Fetches moment_tags where video_offset_seconds IS NULL
    └─> Computes offsets using timestamp_matcher
    └─> Writes offsets back to DB
    └─> Creates moment_clips rows (status: "pending")
    └─> Queues ClipExtractionTask for each
        │
  ClipProcessor (QueueProcessor)
    └─> Extracts 30s clip via trim_video()
    └─> Uploads to Supabase Storage
    └─> Updates moment_clips (status: "ready", storage_url)
```

### New QueueType

Add `CLIPS = "clips"` to the `QueueType` enum. This keeps clip tasks separate
from the video processing queue.

### Directory matching

`ClipDiscoveryProcessor` needs to correlate Supabase `game_sessions.recording_group_dir`
with local group directories. The `recording_group_dir` column stores the group
directory name (e.g., `2026.02.28-14.23.45`). The processor scans local group
directories, checks which have trimmed videos, and queries the DB for matching
game sessions with pending tags.

### Clip output path

Clips are saved to `{group_dir}/clips/moment_{tag_id}.mp4`. The `clips/`
subdirectory is created within each group directory.

---

## Key Files

### New

```
video_grouper/
  task_processors/
    clip_discovery_processor.py   # PollingProcessor — discovers pending tags
    clip_processor.py             # QueueProcessor — extracts + uploads clips
    tasks/
      clips/
        __init__.py
        clip_extraction_task.py   # BaseTask — single clip extraction
```

### Modified

```
video_grouper/
  task_processors/
    queue_type.py                 # Add CLIPS enum value
    register_tasks.py             # Register ClipExtractionTask
    __init__.py                   # Export new processors
  video_grouper_app.py            # Wire ClipDiscoveryProcessor + ClipProcessor
```

### Existing (reused)

```
video_grouper/
  utils/ffmpeg_utils.py           # trim_video(input, output, start_offset, duration)
  models/directory_state.py       # DirectoryState — group dirs, file tracking
  models/match_info.py            # MatchInfo — start_time_offset
  models/recording_file.py        # RecordingFile — start_time, end_time
  services/supabase_client.py     # From SC-1
  services/timestamp_matcher.py   # From SC-2
```

---

## Tasks

### 1. Add CLIPS queue type

- [ ] Add `CLIPS = "clips"` to `QueueType` enum in `queue_type.py`

### 2. ClipExtractionTask

- [ ] Create `video_grouper/task_processors/tasks/clips/__init__.py`
- [ ] Create `video_grouper/task_processors/tasks/clips/clip_extraction_task.py`
- [ ] Extend `BaseTask` as a `@dataclass(unsafe_hash=True)`:
  - `tag_id: str` — moment tag UUID
  - `clip_id: str` — moment clip UUID (created by discovery)
  - `game_session_id: str`
  - `group_dir: str` — local path to recording group
  - `trimmed_video_path: str` — path to trimmed-raw.mp4
  - `clip_start: float` — start offset in seconds
  - `clip_end: float` — end offset in seconds
  - `clip_output_path: str` — output file path
- [ ] `queue_type` → `QueueType.CLIPS`
- [ ] `task_type` → `"clip_extraction"`
- [ ] `get_item_path()` → `clip_output_path`
- [ ] `serialize()` / `deserialize()` — dict round-trip
- [ ] `execute()`:
  1. Create `{group_dir}/clips/` directory if not exists
  2. Call `trim_video(trimmed_video_path, clip_output_path, clip_start, clip_end - clip_start)`
  3. Return True on success, False on failure
- [ ] Register in `register_tasks.py`

### 3. ClipDiscoveryProcessor

- [ ] Create `video_grouper/task_processors/clip_discovery_processor.py`
- [ ] Extend `PollingProcessor` with `poll_interval=60`
- [ ] Constructor: `__init__(storage_path, config, supabase_client, clip_processor)`
- [ ] `discover_work()`:
  1. Scan local group directories (from config `watch_directory`)
  2. For each group dir that has a trimmed video:
     a. Get the group directory name (e.g., `2026.02.28-14.23.45`)
     b. Query Supabase for pending tags matching this group dir
     c. Load DirectoryState to get recording files
     d. Load MatchInfo to get start_time_offset
     e. For each pending tag:
        - Call `compute_combined_offset()` with tag timestamp + recording files
        - Call `compute_trimmed_offset()` with combined offset + start_time_offset
        - If valid offset: update tag in DB, create moment_clip row, queue ClipExtractionTask
        - If invalid: log warning, skip tag
  3. Handle errors gracefully (one bad tag shouldn't block others)
- [ ] Only process groups where trimmed video exists (status check)
- [ ] Skip groups already being processed (track in-progress set)

### 4. ClipProcessor

- [ ] Create `video_grouper/task_processors/clip_processor.py`
- [ ] Extend `QueueProcessor`
- [ ] `queue_type` → `QueueType.CLIPS`
- [ ] Constructor: `__init__(storage_path, config, supabase_client)`
- [ ] `process_item(item: ClipExtractionTask)`:
  1. Execute the task (FFmpeg trim)
  2. On success:
     a. Upload clip to Supabase Storage (`moment-clips/{game_session_id}/{clip_id}.mp4`)
     b. Get public URL from storage
     c. Update `moment_clips` row: status="ready", storage_url=url, file_path=local_path
  3. On failure:
     a. Update `moment_clips` row: status="failed"
     b. Log error
- [ ] `get_item_key(item)` → `f"clip_extraction:{item.clip_id}"`

### 5. Wire into VideoGrouperApp

- [ ] Import ClipDiscoveryProcessor and ClipProcessor in `video_grouper_app.py`
- [ ] Instantiate `SupabaseClient` in `__init__()` (only if `config.supabase.enabled`)
- [ ] Create `ClipProcessor(storage_path, config, supabase_client)`
- [ ] Create `ClipDiscoveryProcessor(storage_path, config, supabase_client, clip_processor)`
- [ ] Add both to `processors` list
- [ ] Call `supabase_client.connect()` in `initialize()` and `close()` in shutdown
- [ ] Feature-gated: only create these processors if Supabase is enabled

### 6. Supabase Storage bucket setup

- [ ] Document the Supabase Storage bucket creation:
  - Bucket name: `moment-clips`
  - Public access for authenticated users
  - Path pattern: `{game_session_id}/{clip_id}.mp4`
- [ ] Add storage upload method to SupabaseClient (if not done in SC-1)

### 7. Unit tests

- [ ] Test ClipExtractionTask serialize/deserialize round-trip
- [ ] Test ClipExtractionTask.execute() with mocked trim_video
- [ ] Test ClipDiscoveryProcessor.discover_work() with mocked DB + file system
- [ ] Test ClipProcessor.process_item() success path (mock execute + upload)
- [ ] Test ClipProcessor.process_item() failure path (mock execute failure)
- [ ] Test feature gating (processors not created when supabase disabled)
- [ ] Test deduplication (same clip not queued twice)

### 8. Integration testing

- [ ] End-to-end test with real FFmpeg: create a short test video, run
  ClipExtractionTask, verify output file exists and is valid
- [ ] Test with simulated recording files and a mock game session

---

## Acceptance Criteria

- ClipDiscoveryProcessor finds pending tags for local game recordings
- Timestamp matcher correctly computes video offsets
- ClipExtractionTask extracts clips via FFmpeg (stream copy, fast)
- Clips are uploaded to Supabase Storage with accessible URLs
- `moment_clips` rows are updated with status and storage_url
- Failed clips are marked as "failed" without blocking the pipeline
- Existing pipeline processors are not affected
- All unit tests pass

---

## Decisions Log

(None yet)
