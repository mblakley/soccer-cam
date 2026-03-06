# Soccer Cam Mobile

Flutter app for processing soccer game video from Dahua IP cameras.

## Features

- Poll Dahua cameras for .dav recording files
- Download with progress tracking
- Combine clips using FFmpeg (stream copy)
- Trim videos with visual start/end markers
- Upload to YouTube via Google OAuth 2.0

## Setup

```bash
flutter pub get
flutter run
```

## Architecture

- **Riverpod** for state management
- **Dio** with HTTP Digest auth for camera communication
- **FFmpegKit** for video processing
- **sqflite** for local job persistence
- Pipeline orchestrator drives state machine per video group
