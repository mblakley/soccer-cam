# Video Grouper System Documentation

## Overview

The Video Grouper system is a comprehensive video processing pipeline designed to automatically capture, process, and upload soccer game recordings from IP cameras. The system implements a multi-stage processing architecture with separate queues for different types of operations, ensuring reliable and scalable video processing.

## System Architecture

The Video Grouper system consists of several interconnected processors, each managing their own queue and responsible for specific aspects of the video processing pipeline:

### Core Processors

1. **CameraPoller** - Discovers new video files on the camera
2. **DownloadProcessor** - Downloads video files from the camera
3. **VideoProcessor** - Handles video processing operations (combine, trim)
4. **AutocamProcessor** - Handles automated video processing with Once Autocam
5. **UploadProcessor** - Manages video uploads to YouTube
6. **NtfyProcessor** - Handles interactive notifications and user input
7. **StateAuditor** - Monitors and audits the overall system state

### Queue Types

The system uses five distinct queue types, each managed by a specific processor:

- **DOWNLOAD** - Video file downloads from camera
- **VIDEO** - Video processing operations (combine, trim)
- **UPLOAD** - Video uploads to external services (YouTube)
- **NTFY** - Interactive notifications and user input requests
- **AUTOCAM** - Automated camera operations

## Video Processing Pipeline

### 1. File Discovery (CameraPoller)

**Frequency**: Every 60 seconds (configurable)

The CameraPoller continuously monitors the IP camera for new video files:

- Polls the camera for files recorded since the last check
- **Filters out files recorded during "connected" periods** (when camera is in office)
- Groups files into directories based on recording time proximity
- Creates `state.json` files to track processing status
- Queues individual files for download

**File Grouping Logic**:
- Files within 5 seconds of each other are grouped together
- Each group gets a directory named with timestamp format: `YYYY.MM.DD-HH.MM.SS`
- Groups are created in the storage directory

### 2. File Download (DownloadProcessor)

**Queue Type**: DOWNLOAD

The DownloadProcessor handles downloading video files from the camera:

- Downloads files one at a time (sequential processing)
- Updates file status in `state.json`:
  - `pending` → `downloading` → `downloaded`
  - Failed downloads are marked as `download_failed`
- When all files in a group are downloaded, queues a **CombineTask** for video processing

**State Tracking**:
- Each file's status is tracked in the directory's `state.json`
- Failed downloads are retried on next StateAuditor cycle

### 3. Video Processing (VideoProcessor)

**Queue Type**: VIDEO

The VideoProcessor handles two main types of video operations:

#### CombineTask
- Combines multiple video files in a group into a single video
- Uses FFmpeg to concatenate files in chronological order
- Outputs a combined video file named `combined.mp4`
- Updates directory status to `combined`

#### TrimTask
- Trims the combined video to game start/end times
- Requires match information (team names, game timing)
- Creates a trimmed video file named `trimmed.mp4`
- Updates directory status to `trimmed`

### 4. Match Information Collection (NtfyProcessor)

**Queue Type**: NTFY

The NtfyProcessor handles interactive notifications to collect match information:

#### Team Information Collection
- Sends notifications asking for team names and location
- Integrates with TeamSnap and PlayMetrics APIs for automatic data retrieval
- Stores information in `match_info.ini` file

#### Game Timing Collection
- Requests game start and end times from users
- Allows users to specify timing offsets for precise trimming
- Supports both manual input and API-based scheduling data

**NTFY Task Types**:
- `TeamInfoTask` - Collects team names and location
- `GameStartTask` - Collects game start time
- `GameEndTask` - Collects game end time

### 5. State Auditing (StateAuditor)

**Frequency**: Every 60 seconds (configurable)

The StateAuditor monitors the entire system and ensures proper workflow:

- Scans all directories for incomplete processing
- Re-queues failed operations
- Triggers next processing steps when prerequisites are met
- Handles cleanup of completed videos

**Audit Actions**:
- Re-queues failed downloads
- Triggers video combining when all files are downloaded
- Initiates match info collection for combined videos
- Queues trimming when match info is complete
- Queues autocam processing for trimmed videos
- Queues uploads for autocam-completed videos

### 6. Autocam Processing (AutocamProcessor)

**Queue Type**: AUTOCAM

The AutocamProcessor handles automated video processing using the Once Autocam GUI application:

- Processes trimmed videos through Once Autocam for automated camera tracking
- Uses GUI automation to control the Once Autocam application
- Applies field marking and zoom settings automatically
- Creates processed videos with automated camera movements

**Autocam Features**:
- **GUI Automation**: Uses pywinauto to control Once Autocam GUI
- **Field Marking**: Automatically marks the playing field boundaries
- **Zoom Settings**: Applies configured zoom and tracking parameters
- **Processing Monitoring**: Waits for completion (up to 6 hours timeout)
- **File Management**: Processes `-raw.mp4` files and outputs `.mp4` files

**Processing Steps**:
1. Launches Once Autocam GUI application
2. Sets source (trimmed video) and destination paths
3. Opens field marking window and applies settings
4. Starts processing and monitors for completion
5. Updates directory status to `autocam_complete` when finished

### 7. Video Upload (UploadProcessor)

**Queue Type**: UPLOAD

The UploadProcessor handles uploading processed videos to YouTube:

- Uploads both raw (combined) and processed (autocam) videos
- Creates playlists based on team configuration
- Sets privacy settings (unlisted by default)
- Updates directory status to `upload_complete`

**Upload Features**:
- Automatic playlist creation and management
- Configurable video titles and descriptions
- Support for multiple YouTube channels
- Error handling and retry logic

## Directory States

Each video group directory progresses through these states:

1. **pending** - Initial state, files being discovered
2. **downloading** - Files are being downloaded
3. **downloaded** - All files downloaded, ready for combining
4. **combined** - Video files combined, waiting for match info
5. **trimmed** - Video trimmed, ready for autocam processing
6. **autocam_complete** - Autocam processing complete, ready for upload
7. **upload_complete** - Upload complete, processing finished

## File Structure

```
storage_path/
├── YYYY.MM.DD-HH.MM.SS/          # Video group directory
│   ├── state.json                # Processing state and file metadata
│   ├── match_info.ini           # Match information (teams, timing)
│   ├── video1.mp4               # Individual video files
│   ├── video2.mp4
│   ├── combined.mp4             # Combined video (after processing)
│   ├── trimmed.mp4              # Trimmed video (after processing)
│   ├── trimmed-raw.mp4          # Raw version for autocam processing
│   └── trimmed.mp4              # Final autocam-processed video
├── youtube/                     # YouTube credentials
│   ├── client_secret.json
│   └── token.json
└── ntfy/                        # NTFY state files
    └── ntfy_service_state.json
```

## Queue Interactions

### Sequential Dependencies

1. **CameraPoller** → **DownloadProcessor**
   - CameraPoller discovers files and queues them for download

2. **DownloadProcessor** → **VideoProcessor**
   - DownloadProcessor queues CombineTask when all files are downloaded

3. **VideoProcessor** → **NtfyProcessor**
   - VideoProcessor completion triggers match info collection

4. **NtfyProcessor** → **VideoProcessor**
   - NtfyProcessor queues TrimTask when match info is complete

5. **VideoProcessor** → **AutocamProcessor**
   - VideoProcessor completion triggers autocam processing

6. **AutocamProcessor** → **UploadProcessor**
   - AutocamProcessor completion triggers upload

### Parallel Processing

- Multiple video groups can be processed simultaneously
- Each processor handles its queue independently
- StateAuditor ensures proper coordination between processors

## Error Handling and Recovery

### Automatic Retry
- Failed downloads are automatically retried
- Failed video processing operations are re-queued
- Upload failures trigger retry attempts

### State Persistence
- All queue states are persisted to disk
- System can recover from crashes and restarts
- Processing resumes from last known state

### Manual Intervention
- Users can manually trigger operations through the tray interface
- Failed operations can be manually retried
- Processing can be paused/resumed as needed

## Configuration

The system is configured through `config.ini` with sections for:

- **Camera settings** (IP, credentials, type)
- **Storage paths** and directory structure
- **YouTube upload settings** (privacy, playlists)
- **NTFY notification settings**
- **API integrations** (TeamSnap, PlayMetrics)
- **Processing intervals** and timeouts

## Monitoring and Control

### Queue Monitoring
- Real-time queue size monitoring
- Processor status tracking
- Processing progress indicators

### User Interface
- System tray application for monitoring
- Configuration management interface
- Manual operation triggers

### Logging
- Comprehensive logging at all levels
- Error tracking and debugging information
- Performance metrics and timing data

## Integration Points

### External APIs
- **TeamSnap** - Team and schedule information
- **PlayMetrics** - Game scheduling and results
- **YouTube Data API** - Video uploads and playlist management
- **NTFY** - Push notifications and user interaction

### External Applications
- **Once Autocam** - Automated video processing and camera tracking

### Camera Systems
- **Dahua IP Cameras** - HTTP/Digest auth, .dav file format, H.264 output
- **Reolink Cameras** - HTTP JSON API + native Baichuan binary protocol (port 9000) for downloads, H.265 output
- **Modular camera registry** - new camera types self-register via ``register_camera()`` in ``cameras/__init__.py``. See ``docs/ADDING_A_CAMERA.md``.

## Performance Considerations

### Resource Management
- Sequential processing prevents resource conflicts
- Configurable processing intervals balance responsiveness and resource usage
- File locking prevents concurrent access conflicts

### Scalability
- Queue-based architecture supports high-volume processing
- Independent processors can be scaled independently
- State persistence enables distributed processing

### Reliability
- Comprehensive error handling and recovery
- State persistence across restarts
- Automatic retry mechanisms for transient failures (3 retries per item)
- YouTube quota exceeded: waits until midnight PT, then retries

### Crash Recovery

The pipeline is designed to recover from crashes at any stage:

- **Queue state**: Persisted atomically (temp file + rename) after every enqueue/dequeue
- **In-progress items**: Tracked in queue state and restored to front of queue on restart
- **state.json**: Written atomically with FileLock; stale locks auto-cleaned after 60s
- **Downloads**: Write to `.tmp` file, rename on success. Crash leaves only a temp file that is cleaned up on restart
- **Combine/Trim**: Write to `.tmp` file, rename on success. Partial output never overwrites a valid file
- **StateAuditor**: Runs once on startup, scans all directories, re-queues interrupted work (including files stuck in "downloading" state)
- **Temp file cleanup**: Orphaned `.tmp` files are removed by StateAuditor on startup