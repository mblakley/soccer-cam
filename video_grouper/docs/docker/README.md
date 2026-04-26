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

## GPU Acceleration (Optional)

The image is GPU-capable: ONNX-based ball detection runs on a CUDA GPU when one is available, otherwise it transparently falls back to CPU. The same image works on GPU and non-GPU hosts — no separate tag.

### Prerequisites for GPU

- NVIDIA driver (Linux) or current NVIDIA Windows driver (for Docker Desktop / WSL2)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed on the host (Linux); Docker Desktop ships it preinstalled in its WSL2 backend (Windows)

### Sanity-check the host can expose its GPU to a container

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04 nvidia-smi
```

If the GPU table prints, the host is configured correctly.

### Run with GPU access

```bash
# docker run
docker run --rm --gpus all video-grouper

# docker compose (already includes the device reservation in docker-compose.yaml)
docker compose up
```

### Enabling ball detection

Ball detection is opt-in via `config.ini`:

```ini
[BALL_TRACKING]
enabled = true
provider = homegrown

[BALL_TRACKING.HOMEGROWN]
device = cuda:0   # use cpu to force the CPU execution provider
```

### Verifying the GPU is actually being used

Once a game reaches the ball-tracking stage, the inference session logs which execution provider it picked:

```bash
docker compose logs video-grouper | grep "ONNX session using"
# GPU host:  ONNX session using: ['CUDAExecutionProvider', 'CPUExecutionProvider']
# CPU host:  ONNX session using: ['CPUExecutionProvider']
```

### Falling back to CPU

Drop the `--gpus all` flag (or set `device = cpu` in `[BALL_TRACKING.HOMEGROWN]`). No code change needed; ORT picks `CPUExecutionProvider` automatically.
