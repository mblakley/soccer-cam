# Data Flow Diagrams

## Pipeline Overview

```mermaid
flowchart TB
    CAM[IP Camera<br/>records to SD card]
    CAM --> CP

    subgraph SERVICE["Windows Service (background)"]
        CP[CameraPoller<br/>polls every 60s]
        DP[DownloadProcessor<br/>downloads to .tmp, renames on success]
        VP1[VideoProcessor<br/>combines clips into one MP4]
        MIS[Match Info Service<br/>1. TeamSnap API<br/>2. PlayMetrics API<br/>3. NTFY fallback]
        NP[NtfyProcessor<br/>sends push notification,<br/>waits for user reply]
        VP2[VideoProcessor<br/>trims to game start/end]
        UP[UploadProcessor<br/>uploads to YouTube]
        SA[StateAuditor<br/>startup recovery scan]

        CP -->|DOWNLOAD queue| DP
        DP -->|all files downloaded| VP1
        VP1 --> MIS
        MIS -->|game found by API| VP2
        MIS -->|no API match| NP
        NP -->|user responds| VP2
        VP2 -->|UPLOAD queue| UP
    end

    subgraph TRAY["Tray App (system tray icon)"]
        SETTINGS[Settings UI]
        MONITOR[Queue monitoring]
        SVCCTL[Service start/stop]
        YTAUTH[YouTube re-auth]
    end

    SERVICE <-->|shared config.ini<br/>shared state.json<br/>shared logs| TRAY
```

## File State Machine

```mermaid
stateDiagram-v2
    [*] --> pending: file discovered by CameraPoller
    pending --> downloading: download starts
    downloading --> downloaded: download succeeds
    downloading --> download_failed: download fails or crash
    download_failed --> downloading: retry (up to 3x)
    downloaded --> [*]: ready for combine
```

## Group State Machine

```mermaid
stateDiagram-v2
    [*] --> pending: group directory created
    pending --> combined: all files downloaded + FFmpeg combine
    combined --> trimmed: match info populated + FFmpeg trim
    trimmed --> autocam_complete: autocam processing (optional)
    autocam_complete --> complete: uploaded to YouTube
    trimmed --> complete: uploaded to YouTube (no autocam)

    combined --> not_a_game: user says no game in recording
```

## Queue Interactions

```mermaid
sequenceDiagram
    participant Camera
    participant CameraPoller
    participant DownloadQ as Download Queue
    participant DownloadProc as DownloadProcessor
    participant VideoQ as Video Queue
    participant VideoProc as VideoProcessor
    participant APIs as TeamSnap / PlayMetrics
    participant NtfyQ as NTFY Queue
    participant NtfyProc as NtfyProcessor
    participant User as User's Phone
    participant UploadQ as Upload Queue
    participant UploadProc as UploadProcessor
    participant YouTube

    CameraPoller->>Camera: get_file_list()
    Camera-->>CameraPoller: [file1.dav, file2.dav, file3.dav]
    CameraPoller->>DownloadQ: queue file1, file2, file3

    loop For each file
        DownloadProc->>DownloadQ: dequeue file
        DownloadProc->>Camera: download_file() to .tmp
        Camera-->>DownloadProc: file data
        DownloadProc->>DownloadProc: rename .tmp to .dav
    end

    DownloadProc->>VideoQ: queue CombineTask
    VideoProc->>VideoQ: dequeue CombineTask
    VideoProc->>VideoProc: combine clips -> combined.mp4

    VideoProc->>APIs: find matching game?
    alt Game found by API
        APIs-->>VideoProc: team names, times
        VideoProc->>VideoQ: queue TrimTask
    else No API match, NTFY enabled
        VideoProc->>NtfyQ: queue GameStartTask
        NtfyProc->>NtfyQ: dequeue task
        NtfyProc->>User: push notification with screenshot
        User-->>NtfyProc: "Yes" / "No"
        NtfyProc->>VideoQ: queue TrimTask with start/end times
    end

    VideoProc->>VideoQ: dequeue TrimTask
    VideoProc->>VideoProc: trim -> final_video.mp4
    VideoProc->>UploadQ: queue YoutubeUploadTask

    UploadProc->>UploadQ: dequeue upload
    UploadProc->>YouTube: upload video
    YouTube-->>UploadProc: video ID
```

## Crash Recovery Flow

```mermaid
flowchart TB
    CRASH[App crashes or power outage]
    RESTART[App restarts]
    SA[StateAuditor runs once]

    CRASH --> RESTART --> SA

    SA --> CLEANUP[Clean up .tmp files<br/>in all group directories]
    SA --> SCAN[Scan all group directories]

    SCAN --> CHK_DL{Files in<br/>pending / downloading /<br/>download_failed?}
    CHK_DL -->|yes| REQUEUE_DL[Re-queue for download]
    CHK_DL -->|no| CHK_COMBINE

    SCAN --> CHK_COMBINE{All files downloaded<br/>but no combined.mp4?}
    CHK_COMBINE -->|yes| REQUEUE_COMBINE[Queue combine task]
    CHK_COMBINE -->|no| CHK_TRIM

    SCAN --> CHK_TRIM{Status = combined<br/>but not trimmed?}
    CHK_TRIM -->|yes| RERUN_MATCH[Re-run match info lookup]
    CHK_TRIM -->|no| CHK_UPLOAD

    SCAN --> CHK_UPLOAD{Status = trimmed or<br/>autocam_complete?}
    CHK_UPLOAD -->|yes| REQUEUE_UPLOAD[Queue upload task]

    subgraph QUEUE_RESTORE[Queue State Restoration]
        QR1[In-progress items restored to front of queue]
        QR2[Queued items restored in order]
        QR3[Retry counts preserved]
    end

    SA --> QUEUE_RESTORE
    QUEUE_RESTORE --> RESUME[Normal processing resumes]
```

## Directory Layout

```
storage_path/
  +-- config.ini                    # Application configuration
  +-- camera_state.json             # Camera connection history
  +-- download_queue_state.json     # Persisted download queue
  +-- video_queue_state.json        # Persisted video processing queue
  +-- upload_queue_state.json       # Persisted upload queue
  +-- ntfy_service_state.json       # NTFY pending requests
  |
  +-- logs/
  |     +-- video_grouper.log       # Application log (rotated daily)
  |
  +-- youtube/
  |     +-- client_secret.json      # Google OAuth credentials
  |     +-- token.json              # Cached OAuth token
  |
  +-- 2026.03.22-09.00.00/          # Video group (one per game)
  |     +-- state.json              # Processing state
  |     +-- match_info.ini          # Game metadata (teams, times)
  |     +-- file1.dav               # Downloaded camera recording
  |     +-- file2.dav
  |     +-- file3.dav
  |     +-- combined.mp4            # All clips joined
  |     +-- trimmed_Eagles_vs_Falcons_2026-03-22_090000.mp4
  |
  +-- 2026.03.15-10.30.00/          # Another game
        +-- ...
```

## Service vs Tray App

```mermaid
flowchart LR
    subgraph SERVICE["Windows Service<br/>(runs at boot, no UI)"]
        S1[CameraPoller]
        S2[DownloadProcessor]
        S3[VideoProcessor]
        S4[NtfyProcessor]
        S5[UploadProcessor]
        S6[StateAuditor]
    end

    subgraph TRAY["Tray App<br/>(runs at login, system tray icon)"]
        T1[Settings UI]
        T2[Queue monitoring]
        T3[Service start/stop]
        T4[YouTube re-auth]
        T5[Recording control]
    end

    subgraph SHARED["Shared Files on Disk"]
        F1[config.ini]
        F2[state.json files]
        F3[queue state files]
        F4[log files]
    end

    SERVICE <--> SHARED
    TRAY <--> SHARED
```

The service does all the work. The tray app is a window into what's happening and a way to change settings. They run independently -- you don't need the tray app for the pipeline to work.
