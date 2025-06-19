# Docker Setup for VideoGrouper

This document explains how to use VideoGrouper with Docker.

## Prerequisites

- Docker
- Docker Compose

## Configuration

1. Create a `config.ini` file in the video_grouper directory of the project (you can copy from `video_grouper/config.ini.dist`)
2. Ensure the `storage_path` in the config points to `/shared_data` (this is mounted as a volume)

## Files Included in Docker Image

The Docker image includes only the necessary files to run VideoGrouper:

- `video_grouper/__init__.py`
- `video_grouper/__main__.py` (consolidated entry point)
- `video_grouper/video_grouper.py`
- `video_grouper/ffmpeg_utils.py`
- `video_grouper/models.py`
- `video_grouper/match_info.ini.dist`
- `video_grouper/cameras/` directory
- Auto-generated `video_grouper/version.py`

## Running with Docker Compose

```bash
# Build and start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down
```

## Building with Version Information

You can specify version information when building:

```bash
docker build -t video-grouper \
  --build-arg VERSION=1.0.0 \
  --build-arg BUILD_NUMBER=123 \
  -f video_grouper/Dockerfile .
```

## Volume Mounts

The Docker setup includes two volume mounts:

1. `./shared_data:/shared_data` - For storing downloaded and processed videos
2. `./video_grouper/config.ini:/app/config.ini` - For the application configuration

Make sure these directories exist and have the correct permissions. 