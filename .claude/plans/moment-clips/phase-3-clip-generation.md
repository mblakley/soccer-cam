# Phase SC-3 — Clip Generation Pipeline

**Status**: NOT STARTED
**Depends on**: SC-1 (API client), SC-2 (timestamp matcher)
**Goal**: Add a ClipDiscoveryProcessor and ClipProcessor to the pipeline that
discover pending moment tags, compute video offsets, extract 30-second clips
with FFmpeg, upload them to Supabase Storage, and update status via the API.

---

## Approach

Two new processors integrate into the existing pipeline:

```
Existing pipeline:
  CameraPoller → DownloadProcessor → VideoProcessor → UploadProcessor
                                          │
New additions (runs after VideoProcessor produces trimmed video):
  ClipDiscoveryProcessor (PollingProcessor, every 60s)
    └─> Calls API: find game sessions matching local recording group dirs
    └─> Calls API: fetch moment_tags with NULL video_offset_seconds
    └─> Computes offsets using timestamp_matcher
    └─> Calls API: update tag offsets + create moment_clip rows
    └─> Queues ClipExtractionTask for each
        │
  ClipProcessor (QueueProcessor)
    └─> Extracts 30s clip via trim_video()
    └─> Uploads to Supabase Storage (direct, via StorageUploader)
    └─> Calls API: update moment_clips (status: "ready", storage_url)
```

### New QueueType

Add `CLIPS = "clips"` to the `QueueType` enum. This keeps clip tasks separate
from the video processing queue.

### Directory matching

`ClipDiscoveryProcessor` correlates local group directories with game sessions
in the API. The `recording_group_dir` column stores the group directory name
(e.g., `2026.02.28-14.23.45`). The processor scans local group directories
that have trimmed videos, and queries the API for matching game sessions with
pending tags.

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
  services/moment_api_client.py   # From SC-1
  services/storage_uploader.py    # From SC-1
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
  - `clip_id: str` — moment clip UUID (created by discovery via API)
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
- [ ] Constructor: `__init__(storage_path, config, api_client, storage_uploader, clip_processor)`
- [ ] `discover_work()`:
  1. Scan local group directories (from config `watch_directory`)
  2. For each group dir that has a trimmed video:
     a. Get the group directory name (e.g., `2026.02.28-14.23.45`)
     b. Call `api_client.get_game_session_by_dir(dir_name)` → game session or None
     c. If no game session, skip this directory
     d. Call `api_client.get_pending_tags(game_session_id)` → list of tags
     e. Load DirectoryState to get recording files
     f. Load MatchInfo to get start_time_offset
     g. For each pending tag:
        - Call `compute_combined_offset()` with tag timestamp + recording files
        - Call `compute_trimmed_offset()` with combined offset + start_time_offset
        - If valid offset:
          - Call `api_client.update_tag_offset(tag_id, offset)`
          - Call `compute_clip_boundaries(offset)` → (start, end)
          - Call `api_client.create_clip(tag_id, game_session_id, start, end, duration)` → clip
          - Queue ClipExtractionTask with clip details
        - If invalid: log warning, skip tag
  3. Handle errors gracefully (one bad tag shouldn't block others)
- [ ] Only process groups where trimmed video exists (status check)
- [ ] Skip groups already being processed (track in-progress set)

### 4. ClipProcessor

- [ ] Create `video_grouper/task_processors/clip_processor.py`
- [ ] Extend `QueueProcessor`
- [ ] `queue_type` → `QueueType.CLIPS`
- [ ] Constructor: `__init__(storage_path, config, api_client, storage_uploader)`
- [ ] `process_item(item: ClipExtractionTask)`:
  1. Execute the task (FFmpeg trim)
  2. On success:
     a. Upload clip via `storage_uploader.upload_file("moment-clips", path, local_path)`
     b. Call `api_client.update_clip(clip_id, status="ready", storage_url=url, file_path=local_path)`
  3. On failure:
     a. Call `api_client.update_clip(clip_id, status="failed")`
     b. Log error
- [ ] `get_item_key(item)` → `f"clip_extraction:{item.clip_id}"`

### 5. Wire into VideoGrouperApp

- [ ] Import ClipDiscoveryProcessor and ClipProcessor in `video_grouper_app.py`
- [ ] Instantiate `MomentApiClient` and `StorageUploader` in `__init__()`
  (only if `config.moment_tagging.enabled`)
- [ ] Create `ClipProcessor(storage_path, config, api_client, storage_uploader)`
- [ ] Create `ClipDiscoveryProcessor(storage_path, config, api_client, storage_uploader, clip_processor)`
- [ ] Add both to `processors` list
- [ ] Close `api_client` and `storage_uploader` in shutdown
- [ ] Feature-gated: only create these processors if moment_tagging is enabled

### 6. Supabase Storage bucket setup

- [ ] Document the Supabase Storage bucket creation:
  - Bucket name: `moment-clips`
  - Public access for authenticated users
  - Path pattern: `{game_session_id}/{clip_id}.mp4`

### 7. Unit tests

- [ ] Test ClipExtractionTask serialize/deserialize round-trip
- [ ] Test ClipExtractionTask.execute() with mocked trim_video
- [ ] Test ClipDiscoveryProcessor.discover_work() with mocked API client + file system
- [ ] Test ClipProcessor.process_item() success path (mock execute + upload + API)
- [ ] Test ClipProcessor.process_item() failure path (mock execute failure + API update)
- [ ] Test feature gating (processors not created when moment_tagging disabled)
- [ ] Test deduplication (same clip not queued twice)

### 8. Integration testing

- [ ] End-to-end test with real FFmpeg: create a short test video, run
  ClipExtractionTask, verify output file exists and is valid
- [ ] Test with simulated recording files and mocked API responses

---

## Acceptance Criteria

- ClipDiscoveryProcessor finds pending tags via the API for local game recordings
- Timestamp matcher correctly computes video offsets
- ClipExtractionTask extracts clips via FFmpeg (stream copy, fast)
- Clips are uploaded to Supabase Storage with accessible URLs
- API is called to update clip status and storage_url
- Failed clips are marked as "failed" via API without blocking the pipeline
- Existing pipeline processors are not affected
- All unit tests pass

---

## Decisions Log

(None yet)
