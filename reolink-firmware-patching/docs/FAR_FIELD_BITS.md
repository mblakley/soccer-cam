# Far-field detail: where the main stream's bits go

**Status (2026-11-27): baseline measured on real game footage. Nothing in this
document has been run on the camera yet.** The camera was disconnected for this
work. Everything under "Measured" comes from archived game recordings and is
reproducible with `verify/stream_quality.py`. Everything under "To test on the
camera" is a plan.

The complaint: the far side of the pitch is blocky. The far ball is 3 to 8
pixels across, so detail lost there is the detail the detector and the viewer
need most.

## Measured

Two full 5-minute main-stream segments from away games, 7680x2160 H.265,
recorded on the 20480 kbps build:

| | Lakefront 2026-06-10 | Niagara Falls 2026-07-12 |
|---|---|---|
| video bitrate | 20.71 Mbps | 20.57 Mbps |
| frame rate delivered | 19.75 fps | 19.62 fps |
| keyframe interval | every 20 frames (1 s) | every 20 frames (1 s) |
| mean keyframe | 894 KB | 1023 KB |
| mean other frame | 90.9 KB | 84.1 KB |
| **keyframes' share of all bits** | **34.1 %** (7.06 Mbps) | **39.0 %** (8.02 Mbps) |
| left for the other 19 frames | 13.65 Mbps | 12.55 Mbps |

Three things follow.

**1. The stream is at its cap during games.** All nineteen full-length
Lakefront segments are 777 to 785 MB. So no setting can add bits; it can only move
them. This confirms the premise of `ENCODER_ROI_QP.md` on game footage; the
14 to 16 Mbps figures in `FIRMWARE_PATCH_NOTES.md` were indoor test scenes.

**2. A third of the bits buy keyframes.** One frame in twenty takes 34 to 39 %
of the stream. A keyframe is ten times the size of the frames between it.

**3. The far field pays for it once a second.** Per-region metrics on the
Lakefront segment, 100 frames from 2:00 in (regions inside the confirmed field
polygon, far touchline at y of about 160 to 330, near at 1060 to 1470):

| region | frames | blk8 | blk16 | blk32 | blk64 | detail |
|---|---|---|---|---|---|---|
| far (2496,180 2816x256) | all | 1.049 | 1.078 | 1.063 | 1.020 | 7.77 |
| | keyframes | 1.059 | 1.090 | 1.118 | 1.085 | 8.70 |
| | last of cycle | 1.049 | 1.076 | 1.047 | 1.012 | 7.36 |
| near (2496,1100 2560x256) | all | 1.025 | 1.033 | 1.054 | 1.053 | 32.08 |
| | keyframes | 1.068 | 1.122 | 1.254 | 1.249 | 34.01 |
| | last of cycle | 1.023 | 1.024 | 1.022 | 1.027 | 32.00 |

`blkN` is the blockiness index on the N-pixel grid (1.0 means no coding
structure). `detail` is mean absolute Laplacian.

- Far-field detail falls **15 %** between a keyframe and the frame before the
  next one (8.70 to 7.36), then snaps back. The near field loses 6 %. This is a
  1 Hz pulse in exactly the region that can least afford it.
- On the 8 and 16 pixel grids the far field carries about twice the near
  field's excess (0.049 and 0.078 against 0.025 and 0.033).
- The far band holds a quarter of the near band's detail, so it is cheap: bits
  moved there go a long way.

## Levers, in the order to try them

### 1. Keyframe interval (no flash)

`GetEnc` reports `gop` with range 1 to 2 for the main stream, as a multiple of
the frame rate. The daily-driver setting in `PATCHING_GUIDE.md` is 1x, and the
footage confirms it: a keyframe every 20 frames.

Setting `gop=2` halves the number of keyframes. If keyframe size stays the same,
that frees 3.5 Mbps (Lakefront) to 4.0 Mbps (Niagara Falls) for the frames
between, **26 to 32 % more** than they get now. This is an upper bound: the rate
controller is free to spend some of it on bigger keyframes instead.

Costs, all bounded:

- Power-cut recovery loses up to one keyframe interval: 2 s instead of 1 s.
- Seeking decodes up to 40 frames instead of 20.
- The 1 Hz pulse becomes a 0.5 Hz pulse.

**UNVERIFIED:** `ReolinkCamera.apply_optimal_settings` sends `gop=40`, which is
outside the 1 to 2 range the camera reports. What the camera does with that
(reject the whole `SetEnc`, clamp it, or ignore the field) has to be read off
the camera. If it rejects, the bitrate and frame-rate changes in the same call
are being lost too.

Going beyond 2 s would need the range lifted in `router`, the same kind of patch
as the bitrate cap. Not located. Not worth looking for until 2x is measured.

### 2. ROI window on the far band (flash)

`ENCODER_ROI_QP.md`. One rectangle with a QP delta. Because the far band is
low-detail, lowering its QP is cheap. Build `4922` is staged; see
`builds/BUILD_LOG.md`.

The rectangle is per-field. The example config assumes the pitch at y 760 to
1660; at Lakefront the far touchline is at y 160 to 330. The pak therefore bakes
a **disabled** default and does nothing until a `roi.conf` is on the card.

### 3. `nr3d` and global AQ strength (no flash)

Open from DECISIONS 2026-06-15: `nr3d` off keeps far more fine texture but adds
noise that costs bits. Worth re-running once levers 1 and 2 have freed some.
`/proc/kdrv_vdocdc/aq` changes global AQ strength live.

## To test on the camera

**The scene must be rate-limited, or every result is a false negative.** A
static indoor scene encodes at about 1.2 Mbps against the 20 Mbps cap
(`BUILD_LOG.md`, rollover measurements). There the encoder already sits at its
QP floor with bits to spare; moving bits around changes nothing. Before any A/B:

```bash
uv run python reolink-firmware-patching/verify/stream_quality.py capture base.mp4 --seconds 30
```

and read the bitrate. It must be at the cap. If it is not, either point the
camera at something busy (game footage looping on a screen that fills the
frame, or outdoors with foliage), or lower `bitRate` with `SetEnc` until the cap
binds. Lock the exposure first: `bash runtime/set_exposure.sh manual 110 20`.

In order. Stop at the first failure.

1. **Identify the build**: `curl http://<cam>/downloadfile/soccercam/build.txt`
   should say `4921 stitchcal`.
2. **Read the encoder's own state**: `cat /proc/hdal/venc/info` over the tcp/2323
   shell (`runtime/camsh.py`). Confirms out 0 is the 7680x2160 path, shows the
   live `gop`, and the QP range.
3. **Keyframe interval A/B**, no flash. `gop=1`, capture 60 s. `gop=2`, capture
   60 s. `stream_quality.py compare`. Expect the keyframe gap to go 20 to 40,
   keyframe share to drop, and far-region detail on the last frame of the cycle
   to rise.
4. **ROI negative control on 4921** (`ENCODER_ROI_QP.md` section 7.1): the stock
   `setroi` must read back `QP = 0`.
5. **Flash 4922.** Confirm `build.txt` names 4922, the stream still encodes, the
   ROI table in `venc/info` is still empty, and `roi.log` says the encoder was
   left untouched.
6. **ROI readback** (section 7.3): `QP = -6, QPMode = 1` in dmesg and a row in
   the ROI table.
7. **ROI A/B**: off, `qp=-6`, `qp=0`, and **`qp=+10` as a positive control**. A
   positive delta makes the rectangle visibly worse even when the encoder is not
   rate-limited, so it proves the window reaches the hardware and settles pixels
   against CTUs independently of the scene. `roi_qp_heatmap.py` for the picture,
   `stream_quality.py compare` for the numbers.
8. Record results in `training/docs/EXPERIMENTS.md`.

A full-game soak (section 7.6) needs a game.

## Not known

| question | how it gets answered |
|---|---|
| Does `gop=2` survive a reboot and a day/night switch | step 3, then reboot |
| Does the rate controller give the freed bits to the frames between, or to bigger keyframes | step 3 |
| What the camera does with `gop=40` from `apply_optimal_settings` | `GetEnc` after an `apply_optimal_settings` run |
| Whether the live RTSP stream and the recorder share one encode path | step 2: `venc/info` lists the paths and their consumers |
| Everything in `ENCODER_ROI_QP.md` section 8 | steps 4 to 7 |
