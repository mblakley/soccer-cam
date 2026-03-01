# Phase SC-2 — Timestamp Matching

**Status**: NOT STARTED
**Depends on**: SC-1 (for type definitions, not runtime)
**Goal**: Build the algorithm that converts a wall-clock tag timestamp into a
video offset within the trimmed game video.

---

## Approach

This is a pure algorithm with no pipeline changes — ideal for thorough unit
testing before integrating into processors.

### The problem

Users tag moments on their phone with UTC wall-clock timestamps. The camera
records multiple .dav files with local-time start/end timestamps. FFmpeg combines
these into `combined.mp4`, then trims it to `trimmed-raw.mp4` based on
`match_info.start_time_offset`. We need to map the wall-clock tag time to a
second offset within the trimmed video.

### The algorithm

```
1. Convert tag UTC timestamp to camera local time (America/New_York)

2. Find which recording file contains the tag:
   - Sort RecordingFiles by start_time
   - Find file where start_time <= tag_time <= end_time
   - If tag falls in a gap between files (up to 5s tolerance), snap to nearest

3. Calculate combined.mp4 offset:
   - Sum durations of all files before the matched file
   - Add (tag_time - file.start_time) as offset within the file
   - combined_offset = sum_prior_durations + offset_within_file

4. Calculate trimmed.mp4 offset:
   - Parse match_info.start_time_offset as seconds
   - trimmed_offset = combined_offset - start_time_offset_seconds
   - If negative, the tag is before the game started → fail

5. Return trimmed_offset (or None if tag can't be matched)
```

### Edge cases

- **Tag in gap between files**: Snap to nearest file boundary (5s tolerance)
- **Tag before first recording**: Return None (can't generate clip)
- **Tag after last recording**: Return None (can't generate clip)
- **Tag near trim boundary**: May produce negative offset → return None
- **Timezone mismatch**: Camera in local time, phone in UTC — convert correctly
- **Clock drift**: 15s clip buffer on each side absorbs ~10s drift
- **No recording files**: Return None

---

## Key Files

### New

```
video_grouper/
  services/
    timestamp_matcher.py     # Pure functions, no side effects
```

### Existing (referenced)

```
video_grouper/
  models/recording_file.py  # RecordingFile.start_time, .end_time
  models/match_info.py       # MatchInfo.start_time_offset, get_start_offset()
```

---

## Tasks

### 1. Create timestamp_matcher service

- [ ] Create `video_grouper/services/timestamp_matcher.py`
- [ ] `compute_combined_offset(tag_utc: datetime, recording_files: list[RecordingFile], camera_timezone: str = "America/New_York") -> float | None`
  - Convert tag_utc to camera local time
  - Sort recording files by start_time
  - Find containing file (with 5s gap tolerance)
  - Sum prior file durations + offset within file
  - Return combined.mp4 offset in seconds
- [ ] `compute_trimmed_offset(combined_offset: float, start_time_offset: str) -> float | None`
  - Parse start_time_offset (HH:MM:SS) to seconds
  - Subtract from combined_offset
  - Return None if result is negative
- [ ] `compute_clip_boundaries(trimmed_offset: float, buffer_seconds: float = 15.0, video_duration: float | None = None) -> tuple[float, float]`
  - clip_start = max(0, trimmed_offset - buffer_seconds)
  - clip_end = trimmed_offset + buffer_seconds
  - If video_duration is known, clamp clip_end to it
  - Return (clip_start, clip_end)
- [ ] Helper: `_find_recording_for_timestamp(tag_local: datetime, files: list[RecordingFile], gap_tolerance_seconds: float = 5.0) -> tuple[RecordingFile, int] | None`
  - Returns (matched_file, index) or None

### 2. Unit tests

- [ ] Test basic offset calculation (tag in middle of a file)
- [ ] Test tag at exact file boundary
- [ ] Test tag in gap between files (within tolerance)
- [ ] Test tag in gap between files (beyond tolerance → None)
- [ ] Test tag before first recording → None
- [ ] Test tag after last recording → None
- [ ] Test timezone conversion (UTC → America/New_York)
- [ ] Test trimmed offset calculation
- [ ] Test negative trimmed offset → None
- [ ] Test clip boundary clamping (near video start)
- [ ] Test clip boundary clamping (near video end with known duration)
- [ ] Test with single recording file
- [ ] Test with many recording files (cumulative offset correctness)
- [ ] Test DST edge case (optional, nice to have)

---

## Acceptance Criteria

- Algorithm correctly converts UTC timestamps to trimmed video offsets
- All edge cases handled gracefully (return None, not crash)
- Timezone conversion is explicit and correct
- 14+ unit tests pass covering all edge cases
- No side effects — pure functions only

---

## Decisions Log

(None yet)
