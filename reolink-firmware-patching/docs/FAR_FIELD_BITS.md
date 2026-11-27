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

## How much bitrate would be enough (estimated 2026-11-27)

**The stream runs at QP 34 to 35.** Read from the slice headers of a 10 s
Lakefront clip (`ffmpeg -bsf:v trace_headers`): keyframes QP 34 to 35, other
frames QP 33 to 35, mean 34.2. The rate controller's allowed range is 25 to 51,
so the stream is nowhere near its floor. The headers also show Main profile,
level 5.0, 64-pixel CTBs, and per-block QP deltas enabled down to 16x16. This is
the frame-level QP; the per-block values after AQ cannot be read with any tool
we have.

**QP 35 is coarse for a far ball.** The quantiser step roughly doubles every 6
QP: about 36 at QP 35, about 18 at QP 29. A ball 3 to 8 pixels wide is a small,
low-contrast blob, and a blob whose transform coefficients fall under the step
is rounded away. **ESTIMATE, not measured:** the far field needs about QP 28 to
29, six steps lower than it gets.

**What that costs, by proxy.** Two seconds of the Lakefront clip, cut into four
full-width bands and re-encoded with x265 at fixed QP 35 and QP 29 (same GOP, no
B-frames, AQ off):

| band (rows) | share of frame | share of bits at QP 35 | cost of QP 35 → 29 |
|---|---|---|---|
| above the far touchline (0–159) | 7 % | 2.5 % | ×1.54 |
| far band (160–479) | 15 % | **4.4 %** | ×1.67 |
| rest of the pitch (480–1471) | 46 % | 45.1 % | ×1.53 |
| foreground below the near touchline (1472–2159) | 32 % | **48.0 %** | ×1.48 |
| whole frame | | | ×1.51 |

- **Uniformly, the answer is 31 to 41 Mbps.** ×1.51 on today's 20.7 Mbps is
  31 Mbps, and that is a floor: the source was already quantised at QP 35, so
  the detail a clean source would add at QP 29 is missing from the estimate.
  The usual rule (bitrate doubles per 6 QP) gives 41 Mbps.
- **For the far band alone, it is under 1 Mbps.** That band takes 4.4 % of the
  bits. Six QP lower costs it ×1.67, about 0.7 Mbps on the proxy, perhaps
  1.5 Mbps on a clean source.
- **Half the stream is spent below the near touchline.** At this field the
  bottom third of the picture is sideline and spectators, and it takes 48 % of
  the bits.

So raising the cap is the expensive way to get there: of every extra bit a
higher uniform bitrate buys, about 4 % lands on the far band. A window on the
far band (lever 2) asks the encoder for the same six QP at a twentieth of the
cost, and a 2 s keyframe interval (lever 1) frees several times what it needs.

Limits of this: one 2 s clip from one game; x265 is not the camera's encoder;
the proxy ignores AQ, which may already favour the flat far band.

## Putting the bits on the field (2026-11-27)

Goal as Mark stated it: most of the stream should go to the field polygon; the
sidelines and sky may look as bad as they like.

**Today the field gets about a third.** Same Lakefront clip, same x265 proxy,
with everything outside the confirmed field polygon blacked out and then the
reverse: the polygon is 35 % of the frame and takes 35 to 37 % of the bits. About
65 % of the stream, some 13 Mbps, is spent outside it.

| region | share of frame | share of bits |
|---|---|---|
| field polygon | 35 % | ~36 % |
| corners: inside the polygon's rows (160–1471) but outside it | 26 % | ~13 % |
| outside those rows (mostly foreground) | 39 % | ~51 % |

**What each mechanism can do with that.** Estimates, assuming the starved area
drops to a third of its present cost:

| mechanism | shape | field's share of bits | status |
|---|---|---|---|
| one ROI window on the field's rows | rectangle | ~36 % → ~60 %, about ×1.7 | build 4922 staged; unproven on hardware |
| ten ROI windows | staircase around the polygon | → ~80 %, about ×2.2 | handler patched (`ROIQP_MULTIWIN=1`, build 4923, `ENCODER_ROI_QP.md` section 4b); executed under qemu, never on hardware. The daemon still drives one window |
| per-block QP map | the polygon itself | slightly better than ten windows | only reachable from inside `device` |
| privacy masks (`SetMask`) | black rectangles | masked area costs almost nothing | no flash; the camera reports the `mask` ability. Count and timing unverified. **The masked picture is gone, not degraded** |

A single rectangle captures most of the gain because most of the waste is one
full-width band of foreground. ×1.7 on the field is about four QP; ×2.2 is about
seven.

**The window has to be set per game.** Across the 23 Reolink games of 2026 with
a field polygon, the far touchline's top row ranges from 110 to 543 and the near
touchline's bottom row from 1283 to 2154. In four games the field runs to the
bottom edge of the frame. No fixed window fits; the horizontal extent barely
varies, so a per-game config is two numbers (top row, bottom row). How those
two numbers reach a camera that records unattended at the field is not solved.

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

### 1b. The encoder's own rate-control commands (no flash) — found 2026-11-27

The debug node that carries `setroi` also carries rate control. From the help
table in `kflow_videoenc.ko` (file offset 0x380d0 onward, read from the
extracted module):

```
setcbr   PathId Enable StaticTime ByteRate FrameRate GOP InitIQp MinIQp MaxIQp
         InitPQp MinPQp MaxPQp IPWeight            => setcbr 0 1 4 524288 30 15 26 10 40 26 10 40 0
setvbr   ...same, plus ChangePos
setevbr  ...plus KeyPPeriod, KeyPWeight, MotionAQStrength and four more
setfixqp PathId Enable IFixQP PFixQP
setaq    PathId Enable I_Str P_Str MaxAq MinAq
```

If these work, one shell session answers three things with no flash and no
rewrite: whether GOP can go past the API's 2 s, whether the QP floor of 25 can
be lowered, and whether the kernel accepts a byte rate above the 20.5 Mbps the
API refuses (which would place that ceiling in the app, not the encoder).

**UNVERIFIED, all of it.** Nobody has written one of these commands on this
camera. Two specific risks: `setroi` on the same node has an argument-width bug
(`ENCODER_ROI_QP.md` section 4) and these may share it; and `device` may
re-apply its own settings over ours. Read the kernel's echo in `dmesg` and
`/proc/hdal/venc/info` after each write, exactly as for `setroi`.

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

### 4. The 20.5 Mbps ceiling itself — traced 2026-11-27, not yet patched

`SetEnc` accepts 20480 kbps and rejects 21504. The camera works in kbps x 1024,
so those are 20,971,520 and 22,020,096 bps. **`device` provisions the main
encode path with a maximum of exactly 20,971,520 (`0x1400000`)**, which is why
20480 is the last value that works: it equals the provisioned maximum.

Checked with objdump against `app_extracted/device` (non-PIE, file offset =
VMA - 0x400000):

```
4983ac  ldr  w2, [x19, #116]       ; which path
4983b0  mov  w3, #0x800000         ; 8,388,608   (the other paths)
4983b8  mov  w0, #0x1400000        ; 20,971,520  (main path)
4983c4  csel w0, w0, w3, eq
4983dc  stp  w0, w3, [sp, #68]     ; into the config struct
49840c  mov  w1, #0x2              ; param 2 = PATH_CONFIG
498414  bl   5cd150                ; hd_videoenc_set
```

A second load of the same constant at `48c098` is stored to `obj+0x2894`, which
the trace found feeding the bitstream-buffer sizing.

**What is NOT established:**

- **The rejecting comparison itself was not found.** The value reproduces the
  boundary exactly, but the instruction that says no (in `device`'s full-config
  path or behind the ioctl in `kflow_videoenc.ko`) is not located. The
  bitrate-only setter at `499510` clamps to the maximum rather than rejecting.
- **Five more loads of `0x1400000`** sit at `55b2a0`, `55b3b4`, `55b438`,
  `55b498` and `55b4cc`. Not characterised. They may be the same limit, or an
  unrelated 20 MiB buffer size.
- **Buffers are sized from this number.** Raising it without the pools that
  depend on it invites an allocation failure or an overrun.

A patch would change `00 28 a0 52` at file offsets `0x983b8` and `0x8c098` (to
`00 40 a0 52` for 32768 kbps, a single-instruction value). **Not built.** It
touches `device`, the process whose death takes the camera off the network, so
it waits on two no-flash readings first: the `PATH CONFIG` row of
`/proc/hdal/venc/info`, and `dmesg` straight after a rejected `SetEnc`, which
should name the check that fired. And EXP-ENC-02 says what it is worth: about
4 % of any extra bitrate lands on the far band.

## Finding the field on the camera (2026-11-27)

The ROI window must follow the field, and the camera records unattended, so the
field has to be found on the camera. `fieldroi/` does that with the project's
existing field-outline model (`training/field_outline`, ResNet18, 10 keypoints),
rewritten as one dependency-free C file and built static for aarch64.

**The model is accurate enough for rows, with padding.** `field_outline_v2` on
five frames from each 2026 Reolink game, against the 14 human-verified polygons
(the model has probably seen most of these games in training, so this flatters
it):

| edge | median error | range | worst single frame |
|---|---|---|---|
| far line (top row) | +19 rows, i.e. too low | -47 to +152 | +179 |
| near line (bottom row) | -93 rows, i.e. too high | -493 to +46 | -642 |

- **Far edge: pad 200 rows upward and all 14 are covered.** Too low is the
  dangerous direction, and padding upward is nearly free (that band is ~4 % of
  the bits).
- **Near edge: weak when the pitch runs to the bottom of the frame.** The four
  worst cases (-184 to -493) are games where the true near edge is row 2154.
  There the model stops short, so the nearest strip of pitch would sit outside
  the window. Near objects are large, so this is the less harmful miss, but it
  is a miss.
- Frame-to-frame scatter is small (far error varies by ~15 rows within a game),
  so the error is a per-scene bias; averaging more frames will not fix it.
- On four July games with no polygon to score against, the overlays look right
  on two inspected by eye (Chili Vortex, Niagara Falls; far line within ~80
  rows).

**The C port matches the model.** `verify/test_fieldroi.sh`: keypoints within
0.25 px of onnxruntime on four games, and the aarch64 build under qemu gives the
same numbers as the host build.

**It refuses blank frames, but only because of a second check.** The network
always answers: shown a uniformly grey or black frame it returns a tidy outline
with ordinary scores. So the tool also requires the claimed field rows to be
grass-coloured (87 to 93 % on real games, 0 % on grey or black).

**Not done, and not known:**

| open | why it matters |
|---|---|
| Time on the camera | 2.3 to 4.1 s single-threaded on a desktop x86. The camera's Cortex-A53 will be several times slower; qemu timing says nothing. Must be measured |
| Memory on the camera | ~20 MB of working memory plus 45 MB of weights mapped read-only from the SD card. Free RAM while `device` runs has not been read |
| Getting a frame | The tool reads the camera's snapshot format: three archived `cmd=Snap` files are baseline 4:2:0 JPEG, 7680x2160, and it decodes them at one-eighth scale from the DC coefficients alone (under 1 MB instead of 50; mean error under 1.4 grey levels against a full decode). Across 27 games the 1/8-scale input moves the far row by a median of 1 (worst 95) and the near row by 15 (worst 141), nearly always outward. **Not wired up:** nothing on the camera fetches the snapshot yet, and whether `cmd=Snap` is safe to call while recording is unmeasured. busybox `wget` is on the camera and `S99_NetState` already uses it against the local API |
| When to run | Not designed. Once at recording start is the obvious choice; the camera may still be being aimed then |
| A window that works at all | Everything here is moot until the ROI window is proven on hardware (levers 2, steps 4 to 7 below) |

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
3b. **Rate-control commands**, no flash (lever 1b). Start with `setcbr` at the
   values `venc/info` already shows, changing only GOP. Then MinPQp. Then
   ByteRate. One change at a time, reading `venc/info` back after each.
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

## Would replacing the firmware remove these limits? (assessed 2026-11-27)

Assessed offline by reading the docs and the extracted firmware; spot-checked
against both. Short version: the limits that are policy live in `device` and
`router`, and the limits that are not policy live in binary kernel modules or
the sensor, which any replacement would have to keep.

- **The SoC is NT98539 (Novatek platform NS02302).** `1_fdt.bin` carries
  `novatek,ns02302`, U-Boot prints `CHIP[NS02302]`, the loader names
  `FW98539A.BIN`, and `device` was built from `cbb/lib/NT98539/`. The "NT9856x"
  in `FIRMWARE_PATCH_NOTES.md` and "NT98530" in `ISF_PARAM_MAP.md` are wrong for
  this unit.
- **Open firmware / own kernel: no path.** OpenIPC's Novatek work lists this
  platform as unsupported ("NS02201/02301/02302/02401 and NA51102 have no
  loader, board trees or QEMU machine to work from", OpenIPC/u-boot PR 99), and
  where it does support Novatek it still runs the vendor's binary media modules.
  It would unlock nothing on the list above.
- **Replacing `device` with our own app: possible, large, and bounded.** It
  would give full encoder control and a recorder that survives power cuts. It
  would not give 30 fps at 7680x2160: `FPS_CEILING.md` puts the sensor readout
  wall at about 26.8 fps. `APP_REPLACEMENT_DESIGN.md` understates the work in
  three ways that later docs establish: HDAL is statically linked into `device`
  and keeps shadow state, so it must be reimplemented rather than replayed;
  `device` also owns the IP address (`BRICK_POSTMORTEM.md`), so a dead app is a
  camera off the network, not "a camera that answers HTTP with no stream"; and
  its "very low risk" first stage is what caused the 4917 outage.
- **Before any of that, lever 1b.** If the debug node's rate-control commands
  work, most of what an app replacement would buy for the encoder is available
  today from a shell.

## Not known

| question | how it gets answered |
|---|---|
| Does `gop=2` survive a reboot and a day/night switch | step 3, then reboot |
| Does the rate controller give the freed bits to the frames between, or to bigger keyframes | step 3 |
| What the camera does with `gop=40` from `apply_optimal_settings` | `GetEnc` after an `apply_optimal_settings` run |
| Whether the live RTSP stream and the recorder share one encode path | step 2: `venc/info` lists the paths and their consumers |
| Everything in `ENCODER_ROI_QP.md` section 8 | steps 4 to 7 |
