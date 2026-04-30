# soccer-cam

[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/mblakley/soccer-cam/main.svg)](https://results.pre-commit.ci/latest/github/mblakley/soccer-cam/main)

Automated recording, processing, and uploading of soccer game videos from IP cameras. Set it up once, and soccer-cam handles the rest -- downloading recordings from your camera, combining clips, trimming to game time, and uploading to YouTube.

## How It Works

```
Camera records game to SD card
        |
   CameraPoller --- discovers new files, groups by timestamp
        |
  DownloadProcessor --- downloads .dav/.mp4 files from camera
        |
   VideoProcessor --- combines clips into one MP4, trims to game time
        |                    |
   NTFY notifications    TeamSnap/PlayMetrics schedule lookup
   (asks you when the     (auto-populates team names and game info)
    game started/ended)
        |
  UploadProcessor --- uploads finished video to YouTube
```

The pipeline is fully automatic after initial setup. It recovers from crashes, retries failed downloads, and waits patiently for your input via push notifications.

## Features

- **Multi-camera support** -- Dahua and Reolink panoramic cameras (180-degree field of view)
- **Automatic file grouping** -- clips recorded within 5 seconds of each other are combined
- **Smart game detection** -- TeamSnap and PlayMetrics integration auto-populates match info
- **NTFY push notifications** -- asks you to identify game start/end times from your phone
- **YouTube upload** -- automatic upload with playlist organization and quota handling
- **Crash recovery** -- persistent state means nothing is lost on restart
- **Web dashboard, config editor, and onboarding wizard** -- runs on loopback at `http://localhost:8765`; same UI on Windows, Linux, and Docker
- **Windows service + minimal tray** -- background service plus a tray icon for AutoCam and dashboard shortcuts
- **Docker support** -- run on Linux with Docker Compose; full UI via the loopback web app
- **Modular camera system** -- add support for new cameras by implementing a simple interface

## Quick Start

### Option 1: Windows Installer

1. Download `VideoGrouperSetup.exe` from the [Releases](https://github.com/mblakley/soccer-cam/releases) page
2. Run the installer
3. The service starts automatically. Open `http://localhost:8765` in your browser, complete the onboarding wizard at `/setup`, and adjust settings later from `/config`. The tray icon's "Open Dashboard" item points at the same URL.

### Option 2: From Source

```bash
git clone https://github.com/mblakley/soccer-cam.git
cd soccer-cam

# Install uv (Python package manager)
# Windows: winget install astral-sh.uv
# macOS: brew install uv
# Linux: curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync --extra tray --extra service

# Create config
cp video_grouper/config.ini.dist shared_data/config.ini
# Edit shared_data/config.ini with your camera IP, storage path, etc.

# Run
uv run python run.py
```

### Option 3: Docker

```bash
docker compose build
docker compose up -d
```

The image is GPU-capable: ball detection auto-detects an available CUDA GPU and otherwise falls back to CPU — the same image runs on either host. To expose a host GPU to the container, use `docker run --gpus all video-grouper` or add the standard NVIDIA `deploy.resources.reservations.devices` block to your compose file. See [video_grouper/docs/docker/README.md](video_grouper/docs/docker/README.md) for prerequisites and verification.

## Configuration

Easiest path: open `http://localhost:8765/setup` in your browser — the onboarding wizard walks you through camera, storage, YouTube, and integration settings. After setup, edit individual fields at `http://localhost:8765/config`.

If you'd rather edit `config.ini` directly, the minimum is:

```ini
[CAMERA.default]
type = dahua          # or "reolink"
device_ip = 192.168.1.100
username = admin
password = your_password

[STORAGE]
path = ./shared_data

[APP]
timezone = America/New_York
```

See `video_grouper/config.ini.dist` for all available options.

### Optional Integrations

**TeamSnap** -- auto-populate match info from your team schedule:
```ini
[TEAMSNAP]
enabled = true
access_token = your_access_token
team_id = your_team_id
my_team_name = Your Team Name
```

**NTFY** -- receive push notifications on your phone:
```ini
[NTFY]
enabled = true
server_url = https://ntfy.sh
topic = your-unique-soccer-cam-topic
```
Install the [NTFY app](https://ntfy.sh) and subscribe to your topic.

**YouTube** -- automatic upload after processing:
```ini
[YOUTUBE]
enabled = true
privacy_status = private
```
Requires Google Cloud OAuth credentials. See `video_grouper/youtube/README.md` for setup.

## Compatible Cameras

| Camera | Type | Output | Status |
|--------|------|--------|--------|
| EmpireTech IPC-Color4K-B180 | Dahua | H.264 .dav | Verified |
| EmpireTech IPC-Color4K-T180 | Dahua | H.264 .dav | Verified |
| Reolink Duo 3 PoE | Reolink | H.265 .mp4 | Verified |

Want to add support for a new camera? See [docs/ADDING_A_CAMERA.md](docs/ADDING_A_CAMERA.md).

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/GETTING_STARTED.md) | End-to-end setup: what to buy, install, configure, and how to record your first game |
| [Data Flow Diagrams](docs/DATA_FLOW.md) | Pipeline diagrams, state machines, queue interactions, crash recovery flow |
| [Hardware Setup](docs/HARDWARE_SETUP.md) | Tripod-mounted camera rig build guide (under $600) |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Common issues: camera offline, disk full, upload failures, crash recovery |
| [Adding a Camera](docs/ADDING_A_CAMERA.md) | Developer guide for contributing new camera type support |
| [Architecture](video_grouper/docs/README.md) | Detailed system architecture for contributors |

## Development

### Setup

```bash
git clone https://github.com/mblakley/soccer-cam.git
cd soccer-cam
uv sync --extra dev --extra tray --extra service
uv run pre-commit install
```

### Testing

```bash
uv run pytest                                    # All tests
uv run pytest tests/test_camera_poller.py        # Single file
uv run pytest -m "not integration and not e2e"   # Unit tests only
uv run pytest -m "integration"                   # Integration tests
```

### Code Quality

```bash
uv run ruff check --fix    # Lint + autofix
uv run ruff format          # Format
```

### Building Installers

```bash
.\build-installer.ps1
```

Creates `VideoGrouperService.exe`, `VideoGrouperTray.exe`, and `VideoGrouperSetup.exe`.

## Project Structure

```
soccer-cam/
├── video_grouper/
│   ├── cameras/               # Camera implementations (Dahua, Reolink)
│   ├── api_integrations/      # TeamSnap, PlayMetrics, NTFY
│   ├── task_processors/       # Pipeline processors and task system
│   ├── web/                   # FastAPI app: dashboard, /config editor, /setup wizard
│   ├── worker/                # Distributed worker entry point (phase 6)
│   ├── tray/                  # Windows tray icon + AutoCam plumbing (PyQt6)
│   ├── service/               # Windows service wrapper
│   ├── utils/                 # Config, FFmpeg, YouTube upload, etc.
│   └── video_grouper_app.py   # Main application orchestrator
├── tests/                     # Unit, integration, and E2E tests
├── docs/                      # User and developer documentation
├── simulator/                 # Camera simulators for testing
└── .github/workflows/         # CI/CD (Docker, Windows, Release)
```

For detailed architecture documentation, see [video_grouper/docs/README.md](video_grouper/docs/README.md).

## CI/CD

- **Docker Build**: Builds and pushes Docker images on code changes
- **Windows Build**: Creates service and tray executables via PyInstaller
- **Release**: Publishes installers when tags are created
- **Pre-commit.ci**: Automated linting and formatting

## License

MIT
