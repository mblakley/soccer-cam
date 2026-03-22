# Getting Started with Soccer-Cam

This guide walks you through everything from buying hardware to watching your first uploaded game video. Follow each step in order.

---

## Step 1: Buy the Hardware

You need a 180-degree panoramic IP camera and a way to mount and power it at the field.

### Minimum Required

| Item | Cost | Notes |
|------|------|-------|
| 180-degree IP camera | $200-300 | See supported models below |
| 128GB+ microSD card | ~$20 | Goes in the camera |
| 16-foot telescoping tripod | $100-150 | Taller is better for viewing angle |
| 12V DC battery pack (5A output) | ~$70 | Powers the camera for 2+ hours |
| DC extension cable (5.5x2.5mm, 16ft+) | ~$10 | Runs power up the tripod |
| Universal pole mount bracket | ~$10 | Attaches camera to tripod head |
| Ethernet cable | ~$5 | Connects camera to your home network after the game |

**Total: ~$400-550**

### Supported Cameras

| Camera | Type | Video Format | Notes |
|--------|------|-------------|-------|
| EmpireTech IPC-Color4K-B180 | Dahua | H.264 .dav | Recommended, verified |
| EmpireTech IPC-Color4K-T180 | Dahua | H.264 .dav | Verified |
| Reolink Duo 3 PoE | Reolink | H.265 .mp4 | Verified, dual-lens |

### Optional

- USB-powered wireless router (~$40) -- lets you preview the camera feed from your phone at the field
- Non-slip drawer liner (~$10) -- prevents camera from sliding on the mount
- Metal pipe strap (~$5) -- secures camera to bracket
- Braided cable sleeve (~$10) -- keeps cables tidy

For detailed hardware assembly instructions, see [HARDWARE_SETUP.md](HARDWARE_SETUP.md).

---

## Step 2: Set Up the Camera

### Physical Setup (One-Time)

1. Insert the microSD card into the camera
2. Mount the camera on the tripod using the bracket and pipe strap
3. Connect the camera to your home network with an ethernet cable
4. Power the camera using the battery pack or a wall adapter

### Camera Configuration (One-Time)

Connect to the camera's web interface (usually `http://<camera-ip>`) and configure:

1. **Static IP address** -- pick an IP on your home network (e.g., `192.168.1.100`) and set it as static. Write this down -- you'll need it for soccer-cam's config.
2. **Auto-record on power** -- the camera should start recording as soon as it powers on.
3. **Highest quality settings** -- maximum bit rate and FPS (typically 25fps).
4. **Disable AI features** -- turn off motion detection, face detection, etc. These reduce frame rate.

---

## Step 3: Install Soccer-Cam

### Option A: Windows Installer (Recommended)

1. Download `VideoGrouperSetup.exe` from the [Releases page](https://github.com/mblakley/soccer-cam/releases)
2. Run the installer. It will ask for:
   - **Storage path** -- where to save videos and config (e.g., `C:\SoccerCam`)
   - **Camera IP** -- the static IP you configured in Step 2
   - **Camera username and password** -- the login credentials for your camera
3. The installer will:
   - Install the background service (starts automatically)
   - Install the system tray app (starts on login)
   - Create your config file
4. You'll see a soccer-cam icon in your system tray -- right-click it to access settings and monitor progress.

### Option B: From Source (for developers)

```bash
git clone https://github.com/mblakley/soccer-cam.git
cd soccer-cam
winget install astral-sh.uv          # install uv package manager
uv sync --extra tray --extra service  # install dependencies

# Create config
cp video_grouper/config.ini.dist shared_data/config.ini
```

Edit `shared_data/config.ini`:
```ini
[CAMERA.default]
type = dahua                  # or "reolink"
device_ip = 192.168.1.100    # your camera's static IP
username = admin
password = your_camera_password

[STORAGE]
path = ./shared_data

[APP]
timezone = America/New_York   # your timezone
```

Run:
```bash
uv run python run.py
```

---

## Step 4: Record a Game

### At the Field

1. Set up the tripod at center field, 15-20 feet outside the touchline
2. Extend the tripod to maximum height
3. Power on the battery pack -- the camera starts recording automatically
4. (Optional) If you have a WiFi router, connect your phone to preview and adjust the camera angle. Both near corners of the field should be visible.
5. Enjoy the game!
6. When the game is over, power off the battery pack

### After the Game

1. Take the camera home
2. Connect the camera to your home network with an ethernet cable
3. Power on the camera (plug in the battery or use a wall adapter)
4. Soccer-cam detects the camera automatically and begins downloading

That's it. From here, the pipeline handles everything:

```
Camera detected --> Files downloaded --> Videos combined --> Game trimmed --> Uploaded to YouTube
                    (~20-40 min)        (~5-15 min)        (you respond     (~10-30 min)
                                                            to notification)
```

---

## Step 5: Identify the Game (Optional but Recommended)

After the video is combined, soccer-cam needs to know when the actual game started and ended (since the camera records warm-ups too). There are three ways this happens:

### Automatic (TeamSnap / PlayMetrics)

If you enable TeamSnap or PlayMetrics integration, soccer-cam looks up your team's schedule and auto-populates:
- Your team name
- Opponent team name
- Game location
- Approximate start/end times

Configure in your `config.ini` or via the tray app settings.

### Semi-Automatic (NTFY Push Notifications)

If you enable NTFY integration:
1. You'll get a push notification on your phone with a screenshot from the video
2. Tap "Yes" if the game has started, or "No" to skip ahead 5 minutes
3. Once you confirm the start, the same process finds the end
4. The video is automatically trimmed

Set up NTFY:
1. Install the [NTFY app](https://ntfy.sh) on your phone
2. Subscribe to a unique topic (e.g., `my-soccer-team-2026`)
3. Add to config: `[NTFY] enabled = true` and `topic = my-soccer-team-2026`

### Manual

Edit the `match_info.ini` file in the video's directory:
```ini
start_time_offset = 03:45
my_team_name = Eagles
opponent_team_name = Falcons
location = home
```

---

## Step 6: Watch Your Video

Once processing completes:

- **YouTube** (if enabled): Videos are uploaded automatically. Check your YouTube Studio for the new uploads.
- **Local files**: Combined and trimmed videos are saved in your storage directory, organized by date.

---

## What's Running on Your Computer

Soccer-cam has two components that run on your Windows PC:

### The Service (Background Processing)

- Runs silently in the background as a Windows Service
- Starts automatically when your computer boots
- Handles all the heavy lifting: polling the camera, downloading files, combining videos, uploading to YouTube
- You never need to interact with it directly

### The Tray App (Control Panel)

- Shows as an icon in your system tray (bottom-right of your screen)
- Right-click to access:
  - **Settings** -- change camera, storage, YouTube, and team settings
  - **Service control** -- start, stop, or restart the background service
  - **Queue status** -- see what's downloading, processing, or uploading
  - **Connection history** -- see when the camera was detected
- The tray app does NOT run the pipeline -- it's just a window into what the service is doing

### How They Work Together

```
Windows Service (background)          Tray App (system tray icon)
   |                                      |
   |-- Polls camera every 60s             |-- Shows queue status
   |-- Downloads .dav files               |-- Lets you change settings
   |-- Combines into MP4                  |-- Start/stop service
   |-- Sends NTFY notifications           |-- YouTube re-authentication
   |-- Uploads to YouTube                 |
   |                                      |
   +-------- shared config.ini -----------+
   +-------- shared state.json files -----+
   +-------- shared log files ------------+
```

They communicate through shared files on disk, not through direct connections. Both can run independently.

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for help with common issues like:
- Camera not found
- Downloads stuck
- Disk full
- YouTube upload failures
- Crash recovery

---

## Next Steps

- **Enable YouTube uploads** -- see the YouTube section in your config or tray settings
- **Enable TeamSnap/PlayMetrics** -- auto-populate game info from your team schedule
- **Enable NTFY notifications** -- get push notifications to identify game start/end times
- **Set up for multiple cameras** -- add `[CAMERA.cam2]` sections to your config
- **Run on Linux** -- use Docker: `docker compose up -d`
