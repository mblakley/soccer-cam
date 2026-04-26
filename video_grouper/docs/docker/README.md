# Docker Setup for VideoGrouper

This document explains how to run VideoGrouper in Docker on Linux (or Windows via Docker Desktop's WSL2 backend). It covers:

- [Quick start](#quick-start) — get the pipeline running, no ball detection
- [Volume mounts](#volume-mounts) — where data goes in and out
- [Ball detection (homegrown ONNX, GPU-capable)](#ball-detection-homegrown-onnx-gpu-capable) — the licensed inference path
- [GPU acceleration](#gpu-acceleration) — when the container should use a CUDA GPU
- [Troubleshooting](#troubleshooting)

---

## Quick start

Bring up the pipeline against your camera with no ball detection:

```bash
# 1. Create a config from the template
mkdir -p shared_data
cp video_grouper/config.ini.dist shared_data/config.ini
# edit shared_data/config.ini: set [CAMERA.default] device_ip / username / password,
# [YOUTUBE] credentials, etc. The default [STORAGE] path = ./shared_data
# is correct -- it resolves to /app/shared_data inside the container.
# Make sure [BALL_TRACKING] enabled = false (default) for now.

# 2. Build (or pull) and run
docker compose build
docker compose up -d

# 3. Watch the logs
docker compose logs -f video-grouper
```

The container polls the camera, downloads new clips, combines/trims them, prompts for game start/end via NTFY, and uploads to YouTube. Standard pipeline.

To rebuild from a specific version tag:

```bash
docker build \
  --build-arg VERSION=1.0.0 \
  --build-arg BUILD_NUMBER=123 \
  -t video-grouper -f video_grouper/Dockerfile .
```

---

## Volume mounts

The default `docker-compose.yaml` mounts one path:

```yaml
volumes:
  - ./shared_data:/app/shared_data
```

The container's working directory is `/app`, and the app's default
shared-data location resolves to `<cwd>/shared_data` = `/app/shared_data`,
so this single mount covers config, state, and tokens.

What lives where:

| Path inside container | Holds |
|---|---|
| `/app/shared_data/config.ini` | Application configuration |
| `/app/shared_data/<game>/` | Per-game video directory (downloaded clips, combined MP4, trimmed MP4, `state.json`, **and the ball-tracking outputs `detections.json` + `trajectory.json` if enabled**) |
| `/app/shared_data/*_queue_state.json` | Persisted async queues (download / video / upload / ball-tracking) |
| `/app/shared_data/ttt/tokens.json` | Cached Supabase access + refresh tokens (see ball detection below) |

**There is no separate "output" volume** — ball detection writes its `detections.json` and `trajectory.json` into the same per-game directory as the input video. Mount whatever directory tree holds your games; outputs land alongside inputs.

If your game videos live elsewhere (e.g., a NAS mount), change `STORAGE.path` in `config.ini` to wherever you mount it (e.g., `/mnt/games`) and add the matching volume to compose.

---

## Ball detection (homegrown ONNX, GPU-capable)

Soccer-cam ships a homegrown ball-detection provider that runs an ONNX model via onnxruntime. The model is **licensed** — fetched at runtime via Team Tech Tools using the user's account, decrypted in memory, and never written to disk. Premium tier required.

### Prerequisites

1. A Team Tech Tools account (`https://teamtechtools.com`) with the ball-detection entitlement.
2. The encrypted model artifact accessible from where the container runs. Public release URLs:
   - `https://github.com/mblakley/soccer-cam/releases/download/model-vX.Y.Z/model-vX.Y.Z.enc`
   - The container fetches this URL automatically — you don't need to download it manually.
3. (Optional) An NVIDIA GPU exposed to the container. CPU works too, just slower; see [GPU acceleration](#gpu-acceleration).

### Configure `config.ini`

Two sections need to be set up. Replace the placeholder values with your TTT credentials.

```ini
[BALL_TRACKING]
enabled = true
provider = homegrown

[BALL_TRACKING.HOMEGROWN]
# Triggers the licensed/encrypted path (vs. local plaintext model_path for testing)
model_key = premium.video.ball_detection
device = cuda:0           # cuda:0 prefers GPU; falls back to CPU automatically. Use 'cpu' to force CPU.
stages = stitch_correct, detect, track, render

[TTT]
enabled = true
supabase_url = https://<your-supabase-project>.supabase.co
anon_key = <your-supabase-anon-key>
api_base_url = https://api.teamtechtools.com
email = <your-ttt-email>
password = <your-ttt-password>
# plugin_signing_public_keys = ["...hex..."]   # optional override; default ships in code
```

Email + password are used for the **first** authentication only — after that, Supabase access + refresh tokens are persisted to `/app/shared_data/ttt/tokens.json` and refreshed automatically. You can clear `password` from `config.ini` once tokens exist if you don't want it stored at rest.

### Headless sign-in via the in-container web server

The container can run a small HTTP server with a status dashboard at `/` that lets you sign in to TTT from a browser without putting credentials in `config.ini`. It supports every method TTT's Supabase project enables:

- **OAuth providers**: Google, Discord, Apple, Facebook, Twitter — buttons redirect to Supabase, which redirects to the provider, which redirects back to the server's `/callback`. The access token is extracted from the URL fragment and persisted to `shared_data/ttt/tokens.json`.
- **Email + password**: an inline form posts to `/login/password`, which calls Supabase's password grant.
- **Magic link**: an inline form posts to `/login/magic`, which asks Supabase to email a sign-in link. Clicking the link in the email lands on the server's `/callback` the same way the OAuth flow does.

The dashboard also shows pipeline queue sizes, camera connectivity, and per-game progress, and refreshes every 10 seconds.

Set in `config.ini`:

```ini
[TTT]
enabled = true
# email and password can be left blank — the OAuth flow populates tokens.json directly.

auth_server_enabled = true
auth_server_bind = 127.0.0.1
auth_server_port = 8765
```

In `docker-compose.yaml`, uncomment the port mapping for the `video-grouper` service:

```yaml
    ports:
      - "8765:8765"
```

Then:

```bash
docker compose up -d
```

Open `http://localhost:8765` in your browser and click the OAuth provider button. On success, `shared_data/ttt/tokens.json` appears with the access token; the rest of the pipeline uses it automatically (restart the container if it was already running, or wait for the next TTT call to pick the file up).

> **Email/password TTT accounts.** If you don't sign in to TTT via an OAuth provider, this server won't help — Supabase's hosted auth UI doesn't cover password sign-in. Use the `[TTT] email` + `password` config.ini fields above instead. After the first call, refresh tokens take over and you can clear `password` from disk if you prefer.

> **Supabase redirect URL allowlist.** The server tells Supabase to redirect back to whatever host you typed in your address bar, e.g. `http://localhost:8765/callback`. That URL must be on the TTT Supabase project's redirect allowlist. `http://localhost:*` is on it for local dev; if you sign in via a non-localhost name (Tailscale, LAN host, etc.), add that URL too — otherwise Supabase rejects the redirect with `redirect_to URL not allowed`.

**Security model.** The auth server is unauthenticated — anyone who can reach the port becomes the signed-in TTT user. The defaults keep that local: `bind = 127.0.0.1` inside the container, exposed only via the explicit Docker port mapping above. Don't change `auth_server_bind` to `0.0.0.0` or expose `8765` on a non-trusted network.

### Run

CPU mode (works everywhere):

```bash
docker compose up -d
```

GPU mode (requires NVIDIA Container Toolkit on the host; Docker Desktop on Windows ships it):

```bash
docker run --rm --gpus all \
  -v $(pwd)/shared_data:/app/shared_data \
  video-grouper
```

Or, to keep using `docker compose` with GPU, append a `deploy.resources.reservations.devices` block to your local `docker-compose.yaml` (it's intentionally not in the default — see [GPU acceleration](#gpu-acceleration)).

### What happens at runtime

When a game reaches the ball-tracking stage:

1. `TTTApiClient` loads tokens from `/app/shared_data/ttt/tokens.json` (or signs in with email/password if no tokens yet).
2. `SecureLoader.acquire("premium.video.ball_detection")` calls `POST {api_base_url}/api/models/premium.video.ball_detection/license` with the JWT.
3. TTT returns a signed license + the AES-GCM key.
4. `SecureLoader` downloads the `.enc` artifact from `artifact_url` (the GitHub release), verifies the SHA-256, and decrypts in memory.
5. The decrypted ONNX model is loaded into `onnxruntime.InferenceSession` with the available execution providers (`[CUDA, CPU]` if GPU exposed, `[CPU]` otherwise).
6. The detect stage runs `detect_balls()` per frame, writing `detections.json` to the game directory.
7. The track stage consumes `detections.json` and writes `trajectory.json`.
8. The render stage produces the broadcast-perspective output video.

Plaintext model bytes never touch the disk.

### Verify it actually loaded the model and picked the right execution provider

After a game runs through, check the logs:

```bash
docker compose logs video-grouper | grep -E "ONNX session using|licensed.*tier"
# Expected on a GPU host:
#   detect: licensed premium.video.ball_detection v0.1.0 (premium, provider=CUDAExecutionProvider)
#   ONNX session using: ['CUDAExecutionProvider', 'CPUExecutionProvider']
# Expected on a CPU host:
#   detect: licensed premium.video.ball_detection v0.1.0 (premium, provider=CPUExecutionProvider)
#   ONNX session using: ['CPUExecutionProvider']
```

And confirm outputs landed in the per-game directory:

```bash
ls -la shared_data/<your-game>/
# detections.json    <- per-frame detections
# trajectory.json    <- smoothed ball track
```

### Local testing without TTT licensing

For development, skip TTT and point at a plain ONNX file:

```ini
[BALL_TRACKING.HOMEGROWN]
model_path = /models/ball_detector.onnx   # plain .onnx, no encryption
device = cuda:0
# leave model_key unset
```

Add a volume mount for the model directory:

```yaml
volumes:
  - ./models:/models:ro
```

This bypasses `SecureLoader` entirely — useful for inference tuning or running against a custom model.

---

## GPU acceleration

The image is GPU-capable. When CUDA is available inside the container, ball detection runs on the GPU. Otherwise it falls back to `CPUExecutionProvider`. **Same image either way** — no `:gpu`/`:cpu` tag split.

### Prerequisites for GPU

- **Linux host:** NVIDIA driver + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).
- **Windows host (Docker Desktop):** current NVIDIA Windows driver. Docker Desktop ships the WSL2 GPU passthrough preinstalled — nothing to install inside WSL.

Sanity-check the host can expose its GPU to a container:

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi
```

If the GPU table prints, the host is configured.

### Adding GPU to compose

The default `docker-compose.yaml` does **not** request a GPU because compose hard-fails on hosts without an NVIDIA runtime. To use GPU under compose, add this to your local override:

```yaml
services:
  video-grouper:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Or pass `--gpus all` to `docker run` directly (works on every host with the toolkit installed).

### Forcing CPU on a GPU host

Two ways:

- Drop `--gpus all` (or remove the `deploy.resources` block) — the container won't see the GPU.
- Set `device = cpu` in `[BALL_TRACKING.HOMEGROWN]` — the inference session is built with CPU-only providers regardless of host.

---

## Troubleshooting

### `nvidia-container-cli: initialization error: WSL environment detected but no adapters were found`

The host doesn't have a usable NVIDIA driver. On Windows, install the latest NVIDIA Game Ready / Studio driver and restart Docker Desktop. Or just run without `--gpus all` — ORT will use CPU.

### `detect: model_key is set but TTT integration is disabled`

`[BALL_TRACKING.HOMEGROWN] model_key` is set but `[TTT] enabled` is `false`. Either flip TTT on (and configure credentials) or use `model_path` instead for local testing.

### `License signature did not validate against any known key`

The container's `plugin_signing_public_keys` doesn't include the key the TTT backend signed the license with. Either:

- The default in code is stale — check for a soccer-cam release that ships the new key, or
- Your TTT instance is signing with a non-default key — set `[TTT] plugin_signing_public_keys = ["<hex>"]` to override.

### `Artifact SHA-256 does not match license manifest`

The `.enc` artifact at `artifact_url` was modified or replaced after the license was issued. Re-acquire (clear `/app/shared_data/ttt/tokens.json` and restart so a fresh license is requested).

### `Configuration file not found at /app/shared_data/config.ini`

The `./shared_data:/app/shared_data` volume isn't mounted, or `config.ini` isn't inside it. Make sure `./shared_data/config.ini` exists on the host before `docker compose up`.

### Container starts but `ONNX session using` never appears in logs

Ball-tracking only runs once a game reaches the `trimmed` state. If you don't have a fully-downloaded game yet, no inference will happen. Check the queue state files in `/app/shared_data/` to see what stage games are at.
