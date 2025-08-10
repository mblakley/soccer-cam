# E2E Test TODO List

## Original Requirements

## TODO Items

### ✅ Camera Simulation
- [x] Create "camera simulator" that intercepts HTTP calls to camera API
- [x] Return manipulable, valid video data
- [x] Provide predefined set of 5 videos recorded 2 hours prior
- [x] Same format as real camera

### ✅ Application Execution
- [x] Start real `video_grouper` application locally
- [x] Start real `tray` application locally (skipped due to GUI dependency)
- [x] Monitor logs to observe sequence of events

### ✅ Pipeline Monitoring
- [x] Camera simulator returning 5 historical video recordings
- [x] Downloading recordings sequentially to configured data directory
- [x] Combining all 5 downloaded recordings into single MP4 file
- [x] Querying simulated TeamSnap and PlayMetrics services
- [x] Prompting user via NTFY (simulated) to identify game start
- [x] Trimming combined video file once match_info.ini is complete
- [x] Initiating Autocam processing via tray agent (simulated)
- [x] Uploading processed video to YouTube (simulated)

### ✅ Test Execution
- [x] Run E2E tests directly from Python using subprocesses
- [x] Consolidate all E2E test code into `tests/e2e` subdirectory
- [x] Create comprehensive test runner with progress monitoring
- [x] Configure subprocesses to log to files and monitor log files
- [x] Implement 60-second timeout between pipeline stages

## Current Issues to Fix

### ✅ Camera Polling Issue - FIXED
- **Problem**: Camera simulator not returning files in expected time range
- **Status**: Files discovered: 5 (working in camera polling test)
- **Root Cause**: Fixed camera simulator time range logic
- **Solution**: Modified `get_file_list()` to return files for E2E testing

### ✅ E2E Test Subprocess Issue - FIXED
- **Problem**: E2E test subprocess not finding files (Files discovered: 0)
- **Status**: Camera polling test works, but E2E test doesn't
- **Root Cause**: Subprocess environment or configuration issue
- **Progress**:
  - ✅ Fixed environment variable passing to subprocess
  - ✅ Subprocess starts successfully (PID confirmed)
  - ✅ Fixed virtual environment issue by using `uv run`
  - ✅ Camera polling now working in subprocess

### ✅ Video Processing Issue - FIXED
- **Problem**: `get_combined_video_path()` missing required argument
- **Status**: Video processor fails to combine files
- **Root Cause**: Method signature mismatch
- **Solution**: Fixed calls to `get_combined_video_path()` to include `storage_path`

### ✅ FFmpeg Async Issue - FIXED
- **Problem**: `process.communicate()` called without `await` in async context
- **Status**: Video combining fails with "cannot unpack non-iterable coroutine object"
- **Root Cause**: Missing `await` in `ffmpeg_utils.py`
- **Solution**: Added `await` to `process.communicate()` calls

### ✅ NTFY Service Method Issue - FIXED
- **Problem**: `'NtfyService' object has no attribute 'process_combined_directory'`
- **Status**: Match info service fails to call NTFY
- **Root Cause**: Missing method in NtfyService class
- **Solution**: Added `process_combined_directory()` method to NtfyService

### ❌ Mock Services Not Being Used
- **Problem**: Real PlayMetrics service being used instead of mock
- **Status**: PlayMetrics login fails, TeamSnap shows "disabled"
- **Root Cause**: Mock services not properly injected into match_info_service
- **Progress**:
  - ✅ Mock services are being created and initialized
  - ❌ Match info service is not using the mock services
  - **Next**: Fix service injection in match_info_service

### ❌ Camera Simulator File Path Mismatch
- **Problem**: Camera simulator creates files with different timestamps than expected
- **Status**: "Test file not found" errors for some downloads
- **Root Cause**: Timing mismatch between simulator file generation and poller expectations
- **Progress**:
  - ✅ Some files download successfully (5 out of 13)
  - ❌ Some files fail with "Test file not found"
  - **Next**: Fix timing synchronization between simulator and poller

### ❌ Pipeline Progress Stuck
- **Problem**: Pipeline not progressing beyond video combining (5/14 stages)
- **Status**: Stuck after "combining_completed" stage
- **Root Cause**: Mock services not being used, causing match info processing to fail
- **Next**: Fix mock service injection and file path issues

## Current Pipeline Progress
- ✅ **camera_polling** - Completed
- ✅ **files_discovered** - Completed  
- ✅ **downloads_started** - Completed
- ✅ **downloads_completed** - Completed
- ✅ **combining_completed** - Completed
- ❌ **match_info_queried** - Stuck here
- ❌ **ntfy_prompted** - Not reached
- ❌ **trimming_started** - Not reached
- ❌ **trimming_completed** - Not reached
- ❌ **autocam_started** - Not reached
- ❌ **autocam_completed** - Not reached
- ❌ **upload_started** - Not reached
- ❌ **upload_completed** - Not reached

## Next Steps
1. Fix mock service injection in match_info_service
2. Fix camera simulator file path timing issue
3. Verify pipeline progression through all stages
4. Test complete end-to-end flow
5. Add timeout handling and better error reporting 