# Hardware Setup Guide

This guide covers the physical hardware needed to record soccer games with soccer-cam.

## Compatible Cameras

### Dahua / EmpireTech 180-Degree Panoramic
- **EmpireTech IPC-Color4K-B180** (verified, recommended)
- **EmpireTech IPC-Color4K-T180**
- 180-degree field of view captures the entire pitch from the sideline
- Records to microSD card in .dav format
- HTTP API for file listing and download
- H.264 video output

### Reolink Duo 3 PoE
- **Reolink Duo 3 PoE** (verified)
- Dual-lens panoramic camera
- Records to microSD card
- H.265 (HEVC) video output
- Downloads use native Baichuan protocol (HTTP download API has a firmware bug)
- Requires `baichuan_port = 9000` in config

### Adding Other Cameras
Soccer-cam has a modular camera system. See [ADDING_A_CAMERA.md](ADDING_A_CAMERA.md) for how to contribute support for new camera types.

## Parts List

| Item | Approx. Cost | Notes |
|------|-------------|-------|
| 180-degree security camera | $200-300 | See compatible cameras above |
| 16' telescoping tripod | $100-150 | Taller = better angle |
| 128GB+ microSDXC card | ~$20 | For camera recording storage |
| 12V DC battery pack (5A + USB) | ~$70 | Powers camera + optional router |
| 16'+ DC extension cable (5.5x2.5mm) | ~$10 | Runs power up the tripod |
| Universal pole mount bracket | ~$10 | Attaches camera to tripod |
| Nut + washer for bracket | <$1 | Secures bracket to tripod mount |
| Non-slip drawer liner | ~$10 | Prevents camera from sliding |
| Metal pipe strap | ~$5 | Secures camera to bracket |
| Ethernet cable | ~$5 | Connects camera to network |
| (Optional) USB wireless router | ~$40 | For field preview on phone |
| (Optional) Braided cable sleeve | ~$10 | Cable management |

**Total: under $600**

## Physical Setup

1. Insert the microSDXC card into the camera
2. Attach the wedge adapter to the top of the tripod using the washer and nut
3. Attach metal pipe strap to one side of the wedge adapter
4. Place non-slip drawer liner on top of the wedge adapter
5. Place the camera on top (right-side up) and secure with the pipe strap
6. Adjust the wedge adapter to angle the camera slightly downward toward the field
7. Connect the DC extension cable between battery pack and camera

### Optional: Field Preview Setup
1. Connect ethernet cable between wireless router and camera
2. Connect USB power from router to battery pack
3. On your phone, connect to the router's WiFi
4. Open a browser and navigate to the camera's IP address
5. Log in and use the live preview to aim the camera

## Camera Configuration

### On the Camera
- Set a **static IP address** (makes config.ini setup easier)
- Enable **auto-record on power-on**
- Use the **highest bit-rate and FPS** available
- **Disable AI features** (object detection, etc.) -- these reduce frame rate

### In soccer-cam config.ini

For a Dahua camera:
```ini
[CAMERA.field]
type = dahua
device_ip = 192.168.1.100
username = admin
password = your_password
```

For a Reolink camera:
```ini
[CAMERA.field]
type = reolink
device_ip = 192.168.1.100
username = admin
password = your_password
baichuan_port = 9000
```

## Game Day Workflow

### Before the Game
1. Set up the tripod at center field, 15-20 feet outside the touchline
2. Extend the tripod to maximum height
3. Power on the battery pack
4. (Optional) Use phone preview to verify the camera captures the full field
5. Adjust camera position if needed -- both near corners should be visible

### After the Game
1. Power off the battery pack (stops recording)
2. At home, connect the camera to your network via ethernet
3. Power on the battery pack to start the camera
4. Run soccer-cam -- it will detect the camera, download recordings, and process them
5. Once downloads complete (check logs or NTFY notification), you can unplug the camera

### Recommended Network Setup
Configure your wireless router to use the same IP range as your home network. This way you can plug the camera directly into your home network after a game without changing any settings.

## Weight Reduction Mod

The camera base is heavy and causes wobble on tall tripods. You can remove it:

1. Cut the ethernet cable near the female connector inside the base
2. Crimp a new RJ45 male connector (10/100 spec -- only 4 wires needed)
3. Pull the cable through the base hole
4. Unbolt the base, remove it
5. Use a male-to-male ethernet adapter to reconnect
6. Reassemble without the base

This significantly reduces wind-induced wobble at maximum tripod height.

## Storage Requirements

- Each hour of 180-degree 4K footage is approximately 5-8 GB
- A typical 90-minute game produces 8-15 GB of raw footage
- After combining and trimming, the final video is smaller
- Recommended: at least 50 GB free on your processing PC
- Configure minimum free space: `[STORAGE] min_free_gb = 2` in config.ini
