# Stitch-seam calibration for the Reolink Duo 3

*Design. Created 2026-08-17, branch `design/stitch-fixer`. Specifies a tool that measures the
dual-lens seam misregistration and corrects it — automatically from footage, or by hand from a
still — emitting one calibration artifact that drives either the camera's warp mesh or the existing
downstream corrector.*

**No camera writes were performed for this document.** Every on-camera figure below came from a
passive read (HTTP `GET`-shaped API calls, `cat` of procfs and config files, `grep` of an installed
binary). The `/proc/hdal/vendor/vpe/cmd` oracle was deliberately *not* used, because issuing it
requires a `write()` to a debug node.

Confidence tiers, following the convention of `training/docs/NONUNIFORM_WARP_DESIGN.md`:
**[M]** measured live in this session, **[R]** read from repo code/docs, **[D]** derived
arithmetically from an [M]/[R] fact, **[I]** inferred with reasoning given.

---

## Why this document lives here

`reolink-firmware-patching/docs/`, not `training/docs/`.

- The bulk of the design is camera-side: the VPE mesh, the `/dev/nvt_vpe` ioctl, procfs geometry,
  the `/mnt/para` config partition, boot hooks in `/etc/init.d`, and shell transport. All of that is
  firmware-patching territory, and it sits next to the tooling it depends on
  (`reolink-firmware-patching/vpe/lut2d.py`, `lut2d_ioctl.c`, on branch `firmware/lut-get-fix`).
- `training/docs/` is scoped by `CLAUDE.md` to training-pipeline state — `STATUS` / `DECISIONS` /
  `EXPERIMENTS` / `GAMES` / `ROADMAP`. `NONUNIFORM_WARP_DESIGN.md` earned its place there because it
  is an *impact assessment on the detection pipeline*. This is a camera calibration procedure; its
  pipeline surface is one optional field on one existing step.
- OSS policy: the repo forbids reverse-engineering of third-party products, but Reolink firmware
  patching is explicitly in scope and already occupies this directory
  (`FIRMWARE_PATCH_NOTES.md`, `APP_REPLACEMENT_DESIGN.md`). Nothing here concerns any other vendor.

Cross-references, not duplicated: mesh format and codec in `../vpe/README.md`; why the parametric
camera-model route cannot specify a mesh exactly, same file; the measured sampling profile and the
seam's importance to detection in `training/docs/NONUNIFORM_WARP_DESIGN.md`.

---

## 1. What is actually there — measured, 2026-08-17

Everything in this section was read from the live unit at 192.168.86.24, firmware line
`v3.0.0.4867_2505072124`, kernel 5.10.168 aarch64.

### 1.1 The media graph

`cat /proc/hdal/vprc/info` **[M]**:

| Stage | Bind | In | Out |
|---|---|---|---|
| `VIDEOPROC 0` | `VIDEOCAP_0_OUT_0` | 3840×2160 RAW12 | 3840×2160 NVX2 |
| `VIDEOPROC 1` | `VIDEOCAP_2_OUT_0` | 3840×2160 RAW12 | 3840×2160 NVX2 |
| `VIDEOPROC 2` (`mode VSP`) | — | 3840×2160 NVX2 ×2 | **out 0: 7680×2160**, **out 1: 256×2160** |

So the panorama is two 3840-wide halves joined at **x = 3840**, and there is no crop: 3840 + 3840 =
7680 exactly **[M/D]**.

### 1.2 `blend_w = 128`, confirmed twice

`grep -a` on `/mnt/app/device` **[M]**:

```
set_proc_cfg(ddr_id, in_dim, frame_num, blend_w, stitch_dim)
all_in_id, (HD_OUT_ID)HD_VIDEOPROC_OUT(dev_id, 1), in_dim, {blend_w * 2, stitch_dim.h}, in_fmt, out_fmt, frc)
Na_set_stitch_v2_param(): bad blend_width %d
cur_reso:%d, media_param.blend_width:%d
```

`VIDEOPROC 2` out port 1 is configured as `{blend_w * 2, stitch_dim.h}`, and it is running at
**256×2160** **[M]**. Therefore `blend_w = 128` and `stitch_dim.h = 2160` **[D]**. The mixed-content
window is **x ∈ [3712, 3968]**.

**Port 1 is produced and nobody reads it** — `OUT WORK STATUS` shows 22 pushed, `USER WORK STATUS`
shows `PULL 0` **[M]**. See §9.1; this is the highest-value open lead in the document.

### 1.3 The vendor scalars, and their persistence

`POST api.cgi?cmd=GetStitch` **[M]**:

```json
"value":   {"stitch": {"distance": 8.0, "stitchXMove": 0, "stitchYMove": 0}},
"initial": {"stitch": {"distance": 8.0, "stitchXMove": 0, "stitchYMove": 0}},
"range":   {"stitch": {"distance":    {"min": 2.0,  "max": 20.0},
                       "stitchXMove": {"min": -100, "max": 100},
                       "stitchYMove": {"min": -100, "max": 100}}}
```

`cat /mnt/para/stitch.cfg` — 203 bytes, mtime 2026-08-16 **[M]**:

```xml
<stitch_set distance="8.0" stitch_x_move="0" stitch_y_move="0" distance_max="20.0"
  stitch_x_move_max="100" stitch_y_move_max="100" distance_min="2.0"
  stitch_x_move_min="-100" stitch_y_move_min="-100" />
```

Three things follow. The scalars persist in the `para` MTD partition (`mtd8`, 2 MB, 960 KB free
**[M]**), so they survive reboot without any help from us. `GetStitch` reports an `initial` block
independent of `value`, so **the factory baseline is recoverable from the camera itself** even if
our artifact is lost — that is the no-shell rollback in §8. And `value == initial` today, so this
unit is at factory settings and nothing in this project has yet moved them **[M]**.

`distance` is capped at **20.0**. If the unit is metres — the Reolink UI presents it as the distance
the stitch is optimised for, but I did not verify the unit **[I]** — then the coarse control
**cannot be set to the depth this sport needs**: the far touchline sits at 60–76 m
(`training/docs/NONUNIFORM_WARP_DESIGN.md`, `world_geometry.py:52-53`, 95×60 m field) **[R]**. That
is the single strongest argument for the mesh path existing at all. §12.1 quantifies the cost.

### 1.4 The mesh

`cat /proc/hdal/vendor/vpe/info` **[M]**:

```
vpe_id_list: 0xF   vpe_idx_num: 4   vpe_2dlut_size: 257
```

Four VPE instances, 257×257 control points. Per `../vpe/README.md` and
`training/docs/NONUNIFORM_WARP_DESIGN.md`, **only VPE 0 carries a live mesh and it warps the left
half**; VPE 1/2/3 read zero **[R]**. I did not re-verify this, because the procfs oracle that would
confirm it (`echo r get_dce_ctl_param …`) is a write. Treat it as the load-bearing assumption it is,
and re-confirm it as step 0 of any implementation.

Control-point spacing, over a 3840×2160 destination half **[D]**:

- horizontal: 3840 / 256 = **15.0 destination px** per control point
- vertical: 2160 / 256 = **8.4375 destination rows** per control point

The brief's "one control point per ~15 destination px" is right for columns but **not** for rows —
the mesh is nearly twice as fine vertically as that suggests. This matters in §5.2.

### 1.5 `CamStitchPara` lives in its own flash partition

`cat /proc/mtd` **[M]**: `mtd11: 00100000 00020000 "stitch"` — a dedicated 1 MB partition. Binary
strings **[M]**: `CamStitchPara`, `CamStitchPara_V3`, `save_stitch_param mtd:%s`,
`get_stitch_param_version from stitch mtd:%s`, `Na_calc_2dlut_data(): stitch param error`,
`delete cam stitch v3 para failed.`

**`mtd11` is this unit's irreplaceable factory calibration and this design never touches it.** The
app can delete it; we must not. Constraint 3 of the brief — never generate a mesh from scratch —
is enforced structurally in §7 by never storing a composed mesh at all.

### 1.6 The seam, measured on a live frame

`cmd=Snap` returns a full **7680×2160** JPEG over plain HTTP in ~2 s, no shell **[M]**. On that
frame, taking column-mean intensity over rows 300–2150 and its smoothed derivative:

- the photometric transition is centred at **x ≈ 3840–3845** across five independent row bands **[M]**
- derivative energy exceeds 5× the local background over **x ∈ [3812, 3868]**, ~56 px **[M]**
- the two halves differ by **~13–33 grey levels** depending on the row band **[M]**

Two readings. First, the seam is where the geometry says it is. Second — and this is a **conflict
with the brief** worth flagging under the "verified facts beat external briefs" rule — the
*photometrically visible* transition is far narrower than the 256-px configured window. The
configured mixing window really is 256 px (§1.2, two independent sources), but the alpha curve puts
most of its energy in the central ~56 px. Design consequence: **measure and gate over the full
configured [3712, 3968]** (conservative, and it is what the hardware mixes), but expect the visible
damage, and the operator's attention, to concentrate in the middle ~56 px.

The brightness step is a separate defect with its own machinery in the firmware —
`adjust_stitch_brightness`, `set_ae_stitch_mode` **[M]**. Geometric calibration does nothing for it
(§12.2).

### 1.7 Transport primitives available on-camera

Probed with `command -v` **[M]**:

| Present | Absent |
|---|---|
| `wget`, `tftp`, `ftpget`, `dd`, `xxd`, `od`, `hexdump`, `uuencode`, `awk`, `sed`, `printf`, `busybox` | `base64`, `curl`, `nc`, `python`, `perl`, `openssl`, `strings` |

The brief is right that there is no `base64` — but **`xxd -r -p` works**, verified by round-tripping
`48656c6c6f0a` → `Hello` on the device **[M]**. Hex is a viable fallback; `wget` is the primary path
(§6.2).

Filesystem **[M]**: `/` is **squashfs, read-only**. `/mnt/app` read-only (`romblock7`).
`/mnt/para` writable, 960 KB free. `/mnt/sda` is the SD card, **238 GB free**.

Shell **[M]**: `/etc/init.d/S36_RootShell` runs `tcpsvd -vE 0.0.0.0 2323 /bin/sh`. There is no login,
no banner, and no prompt — **one command set per TCP connection, stdin read to EOF, then exit**. Any
client must be written for that model, not as an interactive session.

Boot hooks already baked into the rootfs by this project **[M]**: `S35_RecRecover`, `S36_RootShell`,
`S99_NetState`. `S99_NetState` is the pattern to copy: a script fixed in read-only firmware that
reads its mutable configuration from `/mnt/sda/netstate/` **[M]**.

---

## 2. The physical model: a per-row shear, not a translation

The shipped downstream corrector applies a **per-row horizontal roll** — `stitch_remap.py:141-143`:

```python
for y in nonzero:
    dx = int(dx_lookup[y])
    out[y, seam_x:] = np.roll(out[y, seam_x:], dx, axis=0)
```

with `dx` interpolated per row from `[y, dx]` anchors (`build_dx_lookup`, `stitch_remap.py:91-99`)
**[R]**. That shape is not arbitrary. A y-dependent horizontal offset at the seam is the signature of
the two lenses being **rolled relative to each other about their optical axes**, and the existing
format already encodes exactly the right physical model.

Make that concrete. Let the two lenses differ by a roll angle θ. A point at (X, Y) relative to a
half's optical centre displaces by θ·(−Y, +X). At the seam column X ≈ +1920 is fixed while Y runs
over ±1080, so **[D]**:

- **dx = −θ·Y** — linear in y. This is the shear. `dx_anchors` represents it exactly.
- **dy = +θ·1920** — constant in y.

So `dx_anchors` captures the y-varying signature, which is the whole of what a roll does to
horizontal registration. It does not capture the constant vertical offset that the same roll
produces, and no relative-pitch component either. §4.2 handles that as a strictly optional,
mesh-only extension that defaults to absent — the v1 path is untouched by it.

**Rotation and shear are not separately identifiable from seam observations.** Every measurement is
taken at one column, x ≈ 3840. From a single column, a rigid rotation of a half and a linear-in-y
shear of that half produce identical displacements. There is no data to distinguish them, so the
artifact must not pretend to: **the stored model is per-row anchors, never `(tx, ty, rot, shear)`**.
The operator UI may still *offer* translate/rotate/shear as authoring gestures (§5.2) — they are
input devices that write into the anchor curve, not parameters that get stored.

---

## 3. Three correction surfaces, and the precedence between them

| | Surface 1 — vendor scalars | Surface 2 — VPE 0 mesh | Surface 3 — downstream |
|---|---|---|---|
| Mechanism | `SetStitch` over HTTP | `SET_2DLUT` on `/dev/nvt_vpe` | `stitch_correct` step |
| Moves | whole image, both halves | **left half only** | **right half only** |
| Granularity | 3 integers, whole-frame | 257×257 points, **quarter-pixel** | per-row, **integer** |
| Expresses a shear? | **No** | Yes | Yes |
| Acts before blending? | Yes | **Yes** | No |
| Persistence | `/mnt/para/stitch.cfg`, survives reboot | **runtime only** | config file, always |
| Access needed | HTTP | shell + boot hook | none |
| Cost | none | none | full 7680×2160 re-encode |

### 3.1 Precedence: strict fallback, single owner

**The first surface available owns the entire correction. The others contribute nothing.**

```
camera_mesh  >  camera_scalars  >  downstream
```

The temptation is to sum them — coarse on the scalars, fine on the mesh, residual downstream. Reject
that. The mesh is runtime state that dies on reboot (§7), so a split correction silently becomes a
partial correction the moment the camera power-cycles, and nothing in the video says so. A single
owner degrades to *no* correction on failure, which is detectable; a split correction degrades to
*half* a correction, which is not.

One exception, opt-in and explicit: `camera_scalars` may own a coarse integer part with
`downstream` owning the declared residual, because **both** persist unconditionally. That pairing
cannot desynchronise. `camera_mesh` never pairs with anything.

### 3.2 The anti-double-correction mechanism

The artifact carries exactly one authoritative field:

```json
"correction_owner": "camera_mesh" | "camera_scalars" | "camera_scalars+downstream" | "downstream"
```

`stitch_correct` applies `dx_anchors` **if and only if** `correction_owner` names `downstream`.
Otherwise it passes through, exactly as it already does when unconfigured
(`stitch_correct.py:95-99`) **[R]**.

Per `CLAUDE.md` rule 8 — *in automated chains, warnings don't exist* — the ambiguous cases must
**hard-fail**, not log:

- `correction_owner` absent from a profile that carries any `stages[]` block → refuse to run.
- `correction_owner` names `downstream` but `stages[]` records a `camera_mesh` stage in state
  `applied` → refuse to run. That is the double-correct, and it is the one that must never be a
  warning.
- `correction_owner` names `camera_mesh` but `dx_anchors` is non-zero → refuse. A non-zero
  downstream payload under camera ownership is a packaging bug.

A legacy v1 profile — no `correction_owner`, no `stages[]` — is treated as `downstream` and applies
as it does today. Backward compatibility is total (§4.3).

---

## 4. The calibration artifact

### 4.1 `dx_anchors` **is** the artifact

Not a new format wrapping the old one. The existing on-disk `StitchProfile` JSON
(`stitch_remap.py:29-59`) **[R]** is extended in place with optional keys.

This works with **zero code change** to the existing loader, which I verified: `from_dict` reads
only its four keys and ignores everything else **[M]**.

```python
>>> StitchProfile.from_dict({**v1_fields, 'schema': 'seam_calibration/2',
...                          'dy_anchors': [[0, 1]], 'stages': [...]})
StitchProfile(source_width=7680, source_height=2160, seam_x=3840, dx_anchors=[(0, -4), (2159, 3)])
```

`source_width` / `source_height` keep their present meaning and scaling semantics exactly — anchors
are in source-pixel units and `build_dx_lookup` rescales them to the actual frame
(`stitch_remap.py:91-99`) **[R]** — so a profile measured on a 7680-wide still still applies to a
7680-wide video, and to a downscaled one.

### 4.2 Schema

```jsonc
{
  // ---- v1 core: read by the shipped downstream corrector, unchanged semantics ----
  "source_width": 7680,
  "source_height": 2160,
  "seam_x": 3840,
  "dx_anchors": [[0, -6], [540, -3], [1080, 0], [1620, 3], [2159, 6]],

  // ---- v2 additions: ignored by v1 readers ----
  "schema": "seam_calibration/2",
  "calibration_id": "duo3-<serial_hash>-20260817T0412Z",
  "correction_owner": "camera_mesh",

  "sense": {
    // The one paragraph that prevents a sign error. See 4.4.
    "dx_means": "px the RIGHT half must move right, at row y, to register with the left",
    "downstream_moves": "right_half",
    "camera_mesh_moves": "left_half_with_opposite_sense"
  },

  "geometry": {
    "panorama": [7680, 2160], "half": [3840, 2160],
    "blend_w": 128, "blend_window": [3712, 3968],
    "warped_half": "left", "mesh": {"n": 257, "stride": 260, "frac_bits": 2}
  },

  // Optional, mesh-only. Absent unless measured non-negligible. See 2.
  "dy_anchors": null,

  "calibrated_for": {
    "subject_distance_m": 45.0,
    "basis": "far touchline midpoint",
    "fb_px_m": null,                 // f*b, measured per 10.1; predicts residual at any depth
    "residual_px_at": {"8": null, "20": null, "76": null}
  },

  "stages": [
    {"surface": "camera_scalars", "state": "baseline",
     "values":   {"distance": 8.0, "stitchXMove": 0, "stitchYMove": 0},
     "factory":  {"distance": 8.0, "stitchXMove": 0, "stitchYMove": 0}},
    {"surface": "camera_mesh", "state": "applied",
     "baseline_mesh_sha256": "<sha of the mesh dumped AFTER the scalars were set>",
     "max_disp_px": 6.0, "vpe_id": 0},
    {"surface": "downstream", "state": "disabled", "reason": "owned by camera_mesh"}
  ],

  "validation": {
    "metric": "scr_px / ln_ssr",
    "frames_solve": 180, "frames_holdout": 60,
    "before": {"scr_p50": 6.8, "scr_p90": 11.2, "ln_ssr": 0.51, "n_obs": 214},
    "after":  {"scr_p50": 0.7, "scr_p90": 1.4,  "ln_ssr": 0.09, "n_obs": 209}
  },

  "provenance": {
    "workflow": "automated" | "operator",
    "source": "<game_id or snapshot id>",
    "created_utc": "2026-08-17T04:12:00Z",
    "tool_version": "…"
  }
}
```

`dy_anchors` is deliberately `null` by default. It is measurable, the mesh can express it, and a
relative lens roll produces it (§2) — but the downstream surface **cannot**, and inventing a field
that one consumer silently drops is how calibrations become mysterious. Rule: if `dy_anchors` is
non-null and `correction_owner` names `downstream`, the artifact **must** also carry
`"dropped": ["dy_anchors"]` or the tool refuses to write it.

### 4.3 How one artifact drives both paths

```
                     dx_anchors  (+ optional dy_anchors)
                            |
        +-------------------+--------------------+
        |                                        |
  downstream                                camera mesh
  build_dx_lookup(profile, w, h)            for each mesh row v:
  -> int32 dx per row                         y   = dst row of v
  -> np.roll RIGHT half by +dx                d   = interp(dx_anchors, y)
     (stitch_remap.py:83-100, :132-144)       s   = local source-px-per-dst-px
                                              M.x += d * s     # left half, opposite sense
```

Same anchors, same interpolation, two projections. The downstream projection is lossy in three ways
(integer, circular, post-blend — §5.1); the mesh projection is exact to a quarter pixel.

### 4.4 The sign convention, stated once and tested

`dx(y)` means: **the number of pixels the RIGHT half must move to the right, at row y, to register
with the left half.** This is the existing downstream meaning (`stitch_remap.py:88-90`: "`dx>0` moves
the right half right") **[R]**, and keeping it means legacy profiles need no reinterpretation.

The mesh moves the **left** half, so it must realise the *same relative displacement with the
opposite sense*: the left half moves **left** by `dx(y)`.

The mesh stores **source** coordinates for each destination control point, so a destination-space
displacement does not go in directly. For `M(u,v)` = source pixel sampled at destination control
point `(u,v)`, shifting rendered content left by δ destination px means
`M_new(u,v) = M(u + δ, v) ≈ M(u,v) + δ · ∂M/∂u`, and `∂M/∂u` is the **local source-px-per-destination-px
sampling rate** `s(u,v)` **[D]**. Therefore:

```
M_new.x(u, v) = M_factory.x(u, v) + dx(y(v)) * s(u, v)
```

`s` is not 1. It was measured across the field band as **0.63–1.04**, and at the seam specifically
the magnification is 1.47× so `s ≈ 0.68` (`training/docs/NONUNIFORM_WARP_DESIGN.md`) **[R]**.
Ignoring it produces a correction ~47% too large at the seam — enough to look like the tool
overshoots. `s` is computed per control point from the factory mesh itself by finite-differencing
`M.x` along `u`, so it costs nothing and is always current.

**Signed sanity gate, mandatory before any real calibration.** Apply a deliberate `dx = +N` for all
rows (N ≈ 40), measure SCR; apply `dx = −N`, measure again. SCR must move by ≈ ∓N with opposite
signs. If both make it worse, or neither moves it, the sign or the `s` factor is wrong. A sign error
here doubles the seam error instead of closing it, and presents as "the tool doesn't work" — this
gate is the cheapest way to never debug that.

---

## 5. Why bother with the mesh — and where it is worse

### 5.1 Four concrete advantages over `np.roll`

1. **Sub-pixel.** `np.roll` takes an `int` (`stitch_remap.py:115`, `dx = int(dx_lookup[y])`) **[R]**.
   Mesh coordinates are **Q14.2 — quarter-pixel** (`../vpe/README.md`, `lut2d.py` `FRAC_BITS = 2`)
   **[R]**. For a ball 4 px across at the far touchline, "closed to within a pixel" and "closed" are
   different outcomes.

2. **No wraparound.** `np.roll` is circular, and it currently corrupts the frame. Demonstrated
   against the shipped function **[M]**: with `seam_x = 10`, content at the far-right edge
   (x = 17–19) and `dx = +3`:

   ```
   source        x10..19 -> [0, 0, 0, 0, 0, 0, 0, 200, 200, 200]
   dx=+3         x10..19 -> [200, 200, 200, 0, 0, 0, 0, 0, 0, 0]   # far-right content AT THE SEAM
   dx=-3         x10..19 -> [0, 0, 0, 0, 200, 200, 200, 0, 0, 0]   # seam content at the far edge
   ```

   For `dx > 0`, which the docstring documents as legal, the panorama's far-right pixels are
   deposited **directly into the seam columns** — the most detection-critical region of the frame
   (`NONUNIFORM_WARP_DESIGN.md`: hardest ball at x ≈ 3730) **[R]**. The mesh resamples; there is no
   wrap. This is a live bug in shipped code, logged in §11.

3. **Acts before the blend.** The mesh runs in VPE 0, upstream of the VSP (§1.1), so it fixes
   registration *before* the two images are mixed over [3712, 3968]. The downstream surface can only
   slide a strip whose pixels are already a mixture — it can improve geometric continuity but cannot
   restore what mixing destroyed. §9.2 makes that measurable rather than rhetorical.

4. **Free.** The downstream surface decodes and re-encodes a 7680×2160 video
   (`stitch_correct.py:53-75`) with no rate-control setting at all — `add_stream("h264", …)` leaves
   `bit_rate` as `None` **[M]**, so the codec default applies. A generational transcode of the source
   footage, applied by the *first step in the homegrown preset* (`presets.py:49-55`) **[R]**, in
   order to improve detection of a 4-px ball. The camera path costs nothing.

### 5.2 Where the mesh is worse: coarse in y

One control point per **8.4375 destination rows** (§1.4), bilinear between. The downstream path is
genuinely per-row and can express a discontinuity the mesh cannot.

**Does it matter? No, for the physical model in §2 — and this is exact, not approximate.** A relative
lens roll gives `dx` **linear in y**, and a linear function is reproduced *exactly* by linear
interpolation between samples, at any spacing **[D]**. The mesh loses nothing on a roll. It would
only lose a `dx(y)` with structure finer than ~17 rows (Nyquist on an 8.44-row grid), and no rigid
misalignment of two lenses produces that.

**This cannot be settled against real data, because no real profile exists.** The only anchor data in
the repo is a test fixture — `[(0,-10), (477,-20), (657,-35), (1500,0), (2160,0)]`
(`tests/test_stitch_remap.py:23`, `tests/test_ttt_reporter_stitch.py:20`) **[M]** — which is
non-monotone and manifestly synthetic; no camera does that. So the first real measurement must
report it: fit a line to the measured `dx(y)` and record `r²` and the max residual in the artifact.
If the residual is small, the roll model holds and the mesh is lossless. If it is not, that is a
finding about the camera, not about the mesh, and it needs explaining before any correction ships.

### 5.3 The NV12 even-`dx` constraint does not carry over

`stitch_remap.py:118-129` forces `dx_uv = (dx_luma // 2) * 2` so interleaved U/V pairs stay aligned
**[R]**. That is an artifact of shifting a packed NV12 buffer in numpy, and it has a real cost: luma
and chroma can disagree by up to 1 px, which at the seam is a colour fringe on exactly the small
objects that matter.

The mesh has **no such constraint in the format**: `lut2d.py` `_pack` accepts any multiple of 0.25,
and `lut2d_ioctl.c` `table_looks_sane` checks only zero padding and range — no parity check **[R]**.
Chroma resampling is the DCE's job, in hardware, and the VPE output is NVX2 (§1.1). I have **not**
verified that the DCE resamples chroma correctly at odd and fractional offsets **[I]** — that is an
assumption about someone else's hardware. Verification is cheap and belongs in the first on-camera
session: apply a deliberate half-pixel-odd shear to a colour target and look for chroma fringing at
the seam.

---

## 6. Transport

### 6.1 Vendor scalars — HTTP, no shell

`POST /cgi-bin/api.cgi?cmd=SetStitch` with the `value.stitch` object shape returned by `GetStitch`
(§1.3). Persists to `/mnt/para/stitch.cfg` by the firmware's own path — we never write that file.

**`SetStitch` regenerates the mesh.** It feeds `Na_calc_2dlut_data`, the iterative optimiser
(`../vpe/README.md`) **[R]**, so any mesh we previously wrote is destroyed. Ordering is therefore
fixed and not negotiable:

```
1. set scalars  ->  2. wait for the pipeline to settle  ->  3. dump the NEW factory mesh
->  4. compose the correction onto THAT  ->  5. write  ->  6. verify
```

A mesh composed onto a pre-`SetStitch` baseline is wrong. The artifact's
`stages[camera_mesh].baseline_mesh_sha256` exists to catch exactly that: it records the hash of the
mesh the correction was composed against, and the boot hook re-checks it (§7).

### 6.2 Mesh — `wget` pull, primary

The shell is one-shot per connection (§1.7), so streaming 267 KB through it is awkward. Use `wget`,
which is present:

```sh
wget -q -O /mnt/sda/stitchcal/bin/lut2d_ioctl  http://<host>:8642/lut2d_ioctl
wget -q -O /mnt/sda/stitchcal/anchors.txt      http://<host>:8642/anchors.txt
chmod +x /mnt/sda/stitchcal/bin/lut2d_ioctl
```

Binary-safe, one round trip, no encoding. The project already has a static-host precedent on
port 8642. Note that only the **anchors** (a few hundred bytes) are pushed routinely — the binary is
a one-time install, and the mesh itself is never transferred (§7).

**Fallbacks, in order.** (a) Hex over the shell — `base64` is absent but `xxd -r -p` works, verified
**[M]**: `printf '%s' "$HEX" | xxd -r -p > /path`. 267,288 B → 534,576 hex chars, one shot, slow but
certain. (b) `tftp` / `ftpget`, both present **[M]**, for networks where the camera cannot reach an
HTTP host. (c) Physically write the SD card.

### 6.3 The shell client

Written for `tcpsvd … /bin/sh`: connect, write the entire script, **shutdown the write side to
signal EOF**, read until close. Not an interactive session, no prompt matching, no expect loop. One
script per connection; batch aggressively.

---

## 7. Boot persistence

The mesh is runtime state, reprogrammed from `CamStitchPara` on every boot (`../vpe/README.md`)
**[R]**. Re-application is mandatory.

### 7.1 Store the correction, never the composed mesh

**Do not persist a mesh.** Persist the anchors, and compose at every boot against the mesh the
firmware just generated.

This is not a storage optimisation — it is what makes constraint 3 structurally true. "The production
mesh must be the factory mesh composed with a correction" becomes an invariant that cannot be
violated, because the composed mesh has no persistent existence to drift out of date. It also
self-heals: if the scalars change, or a firmware update changes the factory mesh, the next boot
composes onto the new baseline instead of restoring a stale one.

It also settles where things live. `/mnt/para` has 960 KB free **[M]** and holds every device config
— a 267 KB mesh there would be reckless. Anchors are a few hundred bytes.

### 7.2 `S98_StitchCal`

`/etc/init.d` is squashfs read-only **[M]**, so the hook itself requires one firmware build. That is
already this project's normal practice (`S35_RecRecover`, `S36_RootShell`, `S99_NetState`), and the
pattern to copy is `S99_NetState`: **fixed script in firmware, mutable config on the SD card** **[M]**.
Bake it once; every calibration thereafter is a file drop, no reflash.

`S98` runs after the app is up (`S15_NvtAppInit`) and before `S99_NetState` starts recording
decisions — the mesh must be live before the first frame is recorded.

```
/mnt/sda/stitchcal/
    anchors.txt          the correction; the only file routinely updated
    disable              presence => hook exits 0        (rollback, §8)
    watch                presence => enable re-apply polling
    bin/lut2d_ioctl      static aarch64 helper
    factory_boot.bin     this boot's dumped factory mesh (overwritten each boot)
    state.json           witness: what was applied, when, and against which baseline
    log                  rotates at 256 KB, as S99_NetState does
```

Sequence:

1. `[ -e disable ]` → exit 0.
2. **Wait on a condition, never a fixed sleep**: poll `/proc/hdal/vprc/info` until `VIDEOPROC 2`
   reports out 0 at 7680×2160 and its `PUSH` counter is advancing. Timeout → log, exit 0 uncorrected.
3. `lut2d_ioctl get 0 factory_boot.bin`.
4. **Liveness gate before anything else.** Require 66,049 non-zero control points. The empty-read
   failure is real and silent — the driver returns success with zero entries if the argument layout
   is wrong (`../vpe/README.md`, `lut2d.py` `selftest`) **[R]**, and every structural check passes on
   that file. Fail → log, exit 0 uncorrected.
5. `lut2d_ioctl compose factory_boot.bin anchors.txt mesh_out.bin` (§7.3).
6. `lut2d_ioctl set 0 mesh_out.bin --i-have-a-recovery-path`, which already reads back and diffs
   (`lut2d_ioctl.c` `do_set`) **[R]**.
7. Write `state.json`: `{calibration_id, applied_utc, boot_id, baseline_mesh_sha256, readback: "ok"}`.

Every failure path exits **0 and uncorrected**. A camera that records an uncorrected game is a
degraded camera; a camera that fails to boot is a lost weekend.

### 7.3 `lut2d_ioctl compose` — the one new piece of camera-side code

There is no `python` or `perl` on the device **[M]**, so composition must be in the helper that is
already there. New subcommand:

```
lut2d_ioctl compose <factory.bin> <anchors.txt> <out.bin>
```

`anchors.txt` is a plain-text projection of the artifact (the device cannot parse JSON):

```
# seam_calibration/2  <calibration_id>
# baseline_sha256 <hex>
# src 7680 2160  seam 3840
dx 0 -6.00
dx 540 -3.00
dx 1080 0.00
dx 1620 3.00
dx 2159 6.00
```

Per §4.4, for each control point: interpolate `dx` at that row, compute `s` by
finite-differencing the factory mesh along `u`, and add `dx * s` to `M.x`. Then re-apply
`lut2d.py`'s liveness and monotonicity gates before writing — a fold tears the image
(`lut2d.py` `monotonic_fraction`) **[R]**.

Hard refusals: `max|dx| > 64` px (nothing physical needs that; it is a corrupt file), monotonic
fraction below 0.95, any coordinate outside Q14.2 range, or `baseline_sha256` present and not
matching `factory.bin` — the last catching a scalar change that invalidated the correction (§6.1).

### 7.4 `SetStitch` is the other reset

Reboot is not the only thing that destroys the mesh; so does any `SetStitch` (§6.1). The calibration
tool must re-run the mesh stage after touching the scalars. For belt-and-braces, `watch` enables a
low-frequency poll (default 15 min) that re-composes if the live mesh checksum has changed. Off by
default: this is a 240 MB-RAM box **[R]** and polling ioctls is not free.

---

## 8. Rollback

Five tiers, by what access the operator has. Every tier is testable before a calibration is trusted.

| Access | Action | Restores |
|---|---|---|
| **None** | Power-cycle | Mesh only. It is runtime state; the factory mesh returns on boot **[R]**. Scalars persist — use tier 2. |
| **HTTP only** | `SetStitch` with the `initial` block from `GetStitch` | Scalars. **The camera holds its own factory baseline** (§1.3) — recoverable even if the artifact is lost **[M]**. |
| **Physical, no shell** | Pull the SD card, `touch stitchcal/disable` (or delete the dir), reinsert, reboot | Mesh, permanently. `S98` exits 0. |
| **Shell** | `lut2d_ioctl set 0 factory_boot.bin --i-have-a-recovery-path` | Mesh, immediately, no reboot. |
| **Pipeline** | Clear `stitch_profile_path`, or set `correction_owner` away from `downstream` | Downstream. The step is opt-in and passes through (`stitch_correct.py:95-99`) **[R]**; corrected videos are written *alongside* the source (`stitch_correct.py:104`) **[R]**, so the original is never lost. |

A bad mesh **tears the image; it does not brick the camera** (`../vpe/README.md`) **[R]**. The
irreversible action in this whole area is writing `mtd11`, and this design never does (§1.5).

---

## 9. Validation: proving it got better, not just different

Two metrics. They answer different questions and both are needed. Both are computed only over the
field band, and both use structure from **outside** the blend window — never from the mixed pixels
themselves.

### 9.1 Primary — Seam Continuity Residual (SCR), in pixels

For each structure crossing the seam: fit it independently on the left shoulder
`x ∈ [3840−600, 3712)` and the right shoulder `x ∈ (3968, 3840+600]`, extrapolate both to
`x = 3840`, and record the mismatch `r = (r_x, r_y)`. Report `p50`, `p90`, `max`, and `n`.

Report `p90`, not the mean. The mean is dominated by the many easy rows; the seam is a worst-case
problem.

SCR is the magnitude estimator and it is what the solver in §10 minimises. It is honest about the
downstream surface: a downstream shift **does** improve SCR, because it genuinely moves the right
shoulder relative to the left. What it cannot improve is the second metric.

**A better oracle may exist.** `VIDEOPROC 2` out port 1 is a live 256×2160 output that nothing reads
(§1.2) **[M]**, and the binary contains `stitch_ori_snap`, `===>stitch snap yuv[%d] :%s`,
`===>stitch snap jpg[%d] :%s` **[M]** — strings that suggest the app can save pre-stitch, per-sensor
images. If either yields the two source halves separately, registration becomes a **direct two-image
problem** instead of an extrapolation from a fused frame, which is better conditioned in every way.
I did not chase this because pulling an HDAL port and triggering a snap path are not passive reads.
**This is the first thing to investigate in the first on-camera session** — it could simplify the
whole of §10.

### 9.2 Secondary — Seam Sharpness Ratio (SSR), dimensionless

`E(x)` = mean over field-band rows of `(∂I/∂x)²`, computed on the **row-detrended** image
`I(y,x) − Ī(x)`. Fit a smooth background to `E` over the two shoulders, evaluate it across the blend
window, and take `SSR = mean(E / background)` over `x ∈ [3712, 3968]`.

**Report `|ln SSR|`, minimised at 0.** I characterised this empirically rather than assuming it, and
the naive form was wrong twice:

- **Detrending is not optional.** On the live frame, the raw ratio is 3.29 **[M]** — the ~13–33 grey
  level photometric step between the two sensors (§1.6) is a row-independent DC term that swamps the
  structural signal. Detrending removes it by construction.
- **SSR is two-sided, not monotone.** Synthesising a known disparity into a clean, seam-free region
  of the real frame with a 256-px linear alpha **[M]**:

  | disparity px | 0 | 1 | 2 | 4 | 8 | 16 | 32 | 64 |
  |---|---|---|---|---|---|---|---|---|
  | SSR | 0.906 | 0.652 | 0.591 | 0.575 | 0.592 | 0.588 | 0.545 | 0.457 |

  Small disparity **halves each edge** into two ghosts → energy *drops* below 1. Gross disparity
  butts unrelated content together → energy *rises* above 1, which is what the real seam shows
  (structural SSR = 3.19 at ~0.3 m subject distance against an 8 m calibration) **[M]**.

Two consequences, both load-bearing. **SSR saturates**: it cleanly separates 0 px from 2 px but not
4 px from 32 px, so it is a *detector*, not an estimator — SCR does the estimating. And the
background fit has a **noise floor of ~0.10 in `|ln SSR|`** (measured: 0.906 at true zero
disparity), so no threshold tighter than that is meaningful.

**SSR is the metric a downstream shift cannot move.** Post-fusion translation cannot restore energy
that mixing destroyed. That is precisely the "did you improve it or just move it" test:

> SCR improving with `|ln SSR|` flat ⇒ the panorama moved.
> SCR **and** `|ln SSR|` both improving ⇒ registration improved before the blend.

### 9.3 Acceptance gate

All must hold, or the calibration is rejected and the previous state restored:

1. `SCR p90` improves by **≥ 50%**, and lands **< 2.0 px**.
2. `|ln SSR|` does not increase; for a camera-side owner it must **decrease**.
3. `n ≥ 8` accepted structures spanning **≥ 3 row bands** over **≥ 60%** of the field-band height.
4. Measured on **held-out frames** — disjoint from those used to solve. Without this the loop fits
   noise and reports success.
5. The signed sanity gate of §4.4 passed in this session.

---

## 10. Workflow A — automated, from footage

Runnable per venue against games already archived. No camera access is needed to *measure*; access is
needed only to *apply*.

**Detector.** Sample frames across the game. In each, search the two shoulder regions for structures
crossing the seam and fit them:

- *Field lines* are the primary target and the geometry cooperates. On a sideline mount at the
  halfway line, the **far touchline** runs roughly horizontally through the seam — it constrains
  `r_y` well and `r_x` poorly — while the **halfway line** runs nearly vertically through the seam,
  constraining `r_x` well. Between them the seam column is well determined in both axes. Detect with
  a line-segment detector restricted to the two shoulder bands, keep segments whose extrapolations
  both reach x = 3840 within the frame.
- *Non-line edges* (goal frame, netting, fence, far-side buildings, tree line) are admissible under
  the same extrapolate-and-compare primitive, but they sit at uncontrolled depths, so parallax
  contaminates them (§12.1). Accept only where depth can be estimated from the field homography and
  falls inside `calibrated_for.valid_range_m`; down-weight by depth mismatch.
- *Players* are rejected. They move, they straddle depths, and they are the thing being detected.

**Accumulate across the game, not within a frame.** A 90-minute game at 20 fps is ~108,000 frames;
a few hundred good observations is plenty, and no single frame needs to be good.

**Solve.** ~~Weighted robust regression of `dx` on `y`~~ — **corrected in implementation
(`../vpe/stitch_solver.py`, 2026-08-17): there is no per-observation `dx` to regress.** A structure
must span the whole blend window in x to be extrapolated to the seam from both sides, so the usable
ones are *near-horizontal*, and a near-horizontal line of slope `m` displaced horizontally by `dx`
moves **vertically** at the seam:

```
r_y  =  dy  +  m * dx(y)
```

A flat line therefore sees nothing of `dx`, one line under-determines it, and `dx` is recoverable
only **jointly**, from structures of differing slope. Substituting the linear-in-y model of §2,
`dx(y) = a + b*(y − y_ref)`, gives one three-parameter fit over every accumulated observation, with
design row `[1, m, m*(y − y_ref)]` and unknowns `(dy, a, b)`. Huber loss; weights are the inverse of
each observation's extrapolation variance (below). Emit anchors by sampling the fitted line at 5
rows. Record `r²` and the max residual (§5.2), and test a quadratic term — if it is significant, that
is a finding about the camera and goes in the artifact, but the emitted curve stays straight until a
human has looked at it. **[M]**

What the fit can and cannot separate, stated so the artifact does not overclaim:

- It **does** separate translate (`a`) from shear (`b`) — that needs spread in `m*(y − y_ref)`, i.e.
  differing slopes at differing heights, which is a property of the *design matrix*, not of `n`.
- It **does not** separate a rigid relative rotation from a linear-in-y shear (§2): both are `dx`
  linear in y observed at one column. `roll_theta_rad` in the metadata is the roll *interpretation*
  of `b`, not an independently measured angle.
- It **does not** attribute the misregistration to a half. `dx` is relative, which is all either
  surface needs.

**Weights, and what the blend window costs.** Every observation is extrapolated from a shoulder
across at least `blend_w/2 = 128` px of already-mixed pixels. For a chain of length `L` seeded at the
blend edge, the fit's centroid is `D = 128 + L/2` from the seam, and the variance of a linear
extrapolation is `sigma^2/L * (1 + 12 D^2 / L^2)` per side, doubled because `r_y` is a difference
**[D]**. That is ~9× the in-span variance for a full 384-px chain and ~90× for a 96-px one, so
weighting by it — rather than by chain length or fit quality alone — is what stops a handful of
scraps outvoting the good data. It bounds *precision*, not correctness; what is irrecoverable is the
ghosting already fused into the window, which is what SSR measures (§9.2).

**Free consistency check.** §2 says the same roll `theta` that shears `dx` must also produce a
constant `dy = theta * 1920` at the seam. The fit estimates `dy` anyway, as a nuisance parameter, so
comparing it to `b * 1920` costs nothing and is a check on the *physics* rather than the algebra. A
disagreement means something other than roll contributes — relative pitch, or parallax at a depth
away from the calibration (§12.1) — and is reported as a finding. `dy` itself is still not emitted:
the downstream surface cannot express it (§4.2, §12.2).

**Sign, once more, because two modules disagree by design.** `seam_metric.ScrResult.implied_dx`
models `r_y = dy − m*dx` and so reports the misregistration the right half currently *carries*; the
artifact's `dx` is the *correction*, "px the right half must move right" (§4.4). Equal and opposite.
The solver fits the correction directly rather than negating anything, and a test pins the
relationship. **[M]**

**Converge.** Apply → re-measure on held-out frames → iterate. Stop when `SCR p90 < 1.0 px`, or the
improvement between iterations is `< 0.2 px`, or after 4 iterations. **Revert-on-regression**: if an
iteration worsens `SCR p90`, restore the previous mesh and stop. Non-convergence after 4 iterations
is a *reported failure*, not a shipped best-effort.

**Failure mode: no usable structure at the seam.** This is the expected case, not the exotic one —
the seam sits mid-field, and mid-field is featureless grass. Handling, in order:

1. Widen the sample across the whole game before concluding anything.
2. If observations are still below the §9.3 threshold, **hard-fail: emit no calibration, exit
   non-zero.** Do not extrapolate from three observations at one height. Per `CLAUDE.md` rule 8, an
   under-determined fit that logs a warning and writes a profile anyway is the worst outcome
   available — it is wrong and it looks finished.
3. Emit a **measurement report** instead: every observation found, with rows and residuals, so the
   operator starts Workflow B from real numbers rather than from zero.
4. Documented recovery: a **deliberate target**. Stand a high-contrast vertical edge — a taped board,
   or just walk the goal to the halfway line — so it straddles the seam at roughly the calibration
   depth, and grab a 10-second clip. One decisive observation set, ~2 minutes of a volunteer's time.

**§9.3's counts are necessary but not sufficient — corrected in implementation.** The coverage
conditions (`n ≥ 8`, ≥ 3 row bands, ≥ 60% of height) are all *proxies* for a design matrix that pins
the curve, and a case exists that satisfies every one of them and is still unusable: structures
spread over the full height and all three bands, but with slopes so shallow that `dx` — which enters
only as `m*dx` — has almost no lever. Demonstrated with 18 observations across three bands over 70%
of the height at slopes ±0.011, where the fitted `dx` is uncertain to ±1.35 px **[M]**. So the
governing gate is the **standard error on `dx` at the anchor rows**, refused above 1.0 px, which is
the target §10 stops iterating at: a fit whose own uncertainty exceeds the accuracy it is aiming for
has not measured anything. The count and coverage conditions are kept as well, because they give a
far better error message and because passing them means `check_acceptance` cannot later reject the
same solve on coverage grounds. The solver reports **per-band observation counts** in every result,
accepted or refused, so a caller can see which of these bit.

### 10.1 What 27 archived games actually contained — measured 2026-08-17

The solver was run against the Duo 3 archive on `DESKTOP-5L867J8`: every `2026.*` directory under
`F:\Heat_2012s\` and `F:\Flash_2013s\` holding `RecM09_*.mp4`, which is 27 games and ~400 segments.
Four frames per game were extracted server-side, one per segment at t = 30 s, spread across the game
(`F:\archive\duo3_stitch\solver_frames\`), 96 frames total, each verified 7680×2160 before use
**[M]**. 27 distinct tripod placements, indoor domes and outdoor pitches.

**All 27 games refused. None produced a calibration.** That is the headline, and it is a result about
the *observation primitive*, not about the fit.

| What was measured | Value |
|---|---|
| Observations found per game | 40–498 (4 games: 0, single-segment dirs) |
| Fraction of observations in the **top** row band | 73–98%; smallest band's share ≤ 9.2% in every game |
| Top-band share **per frame** | 0.48–1.00, median 0.87 — stable across frames within a game **[M]** |
| Chain span | median **68 px** against a 384-px shoulder; `min_len=150` returns **zero** observations |
| `r_y` scatter inside one 150-row slice | 7–20 px, against a ~1.3 px per-observation noise model |
| `corr(r_y, slope)` inside a slice | \|0.04\|–\|0.28\|; per-slice implied `dx` −58…+91 px, incoherent |
| Weighted `r²` of the joint fit | **0.00–0.24** across all 27 games |
| Robust dispersion | 5–17× the noise model |
| `\|ln SSR\|` over the 96 frames | median **0.093**, p90 0.390; **52/96 below the 0.10 noise floor** |

Read together:

1. **Usable near-horizontal structure lives only on the far touchline and the crowd behind it.** Below
   about row 900 of 2160 the frame is grass, and grass produces no edge that chains for 60 px on both
   shoulders. This is the failure mode §10 predicts, and it is worse than predicted: it is not
   "sometimes there is nothing", it is *every game, every frame*.
2. **More frames do not fix it.** The imbalance is per-frame and stable, so accumulating across the
   whole game — §10's remedy — adds mass to the same band. Checked, not assumed **[M]**.
3. **The residuals do not describe a per-row `dx`.** With `r²` ≈ 0 and almost no correlation between
   `r_y` and slope within a row slice, whatever produces the 7–20 px scatter (spurious left/right
   pairings, moving people, objects at differing depths sharing a row) is not a common shear.
4. **There may be little to correct.** More than half the frames are below the SSR noise floor, i.e.
   indistinguishable from a registered seam at the ~1 px level. Only the indoor dome game
   (2026.03.21) is consistently high (0.405–0.533). SSR saturates past ~4 px so this is not proof of
   sub-pixel registration, but it is evidence against a gross one.

**Consequence for the design: §10 step 4 is the primary path, not the fallback.** A deliberate target
straddling the seam at calibration depth is the only route this footage supports. The automatic
solver's job on archived footage is to *say so*, with numbers, which is what it now does.

**And a gate §9.3 was missing.** `height_coverage` is a *range* — `(y_max − y_min) / band` — so two
stragglers at the extremes satisfy it. On this archive, range coverage read 81–98% on 24 of 27 games
while 73–98% of the mass sat in one band **[M]**. Leverage on a shear comes from mass at differing
heights, so the solver adds `MIN_ROW_BAND_FRACTION = 0.10`: every row band must hold at least a tenth
of the observations. With it, 23 of the 27 refusals name the actual problem ("piled into one band")
instead of reporting an uninterpretable ±50–375 px error bar.

**Limitations of this run, stated.** Four frames per game is a thin sample of ~108,000 (finding 2 is
why that is defensible, not an excuse). The frames are single stills, so no within-game drift over
time was measured. And no calibration was applied to any camera, so nothing here is an end-to-end
before/after.

### 10.2 Verdict: SCR cannot be the objective for an automatic solver on fused frames

This is the load-bearing correction to the design, and it is measured.

**Root cause, in one line: on a soccer field almost everything that crosses a vertical seam runs
horizontally, and a horizontal edge is invariant under a horizontal shift.**

`dx` enters every SCR observation only as `m · dx` (§10). So the lever is the structure's slope. Over
**4239 observations from the 27-game archive** **[M]**:

| | median | p90 | max |
|---|---|---|---|
| \|slope\| | **0.034** | 0.089 | 0.265 |

A 10 px misregistration therefore moves the median observation by **0.34 px** vertically — below the
~1.3 px noise on one observation. And this is not fixable by tuning: a structure must span the 256-px
blend window in x to be extrapolated to the seam from both shoulders, which is *why* only
near-horizontal ones qualify, and `seam_metric` enforces `|m| ≤ 0.35` accordingly. The admissible
feature set and the informative feature set barely overlap.

**The discriminative sweep, which is the check with teeth.** Apply a constant `dx`, re-detect, and
look at whether the score moves. Three frames, chosen by vertical-edge energy in the blend window —
two of the richest, one of the poorest **[M]**:

| dx | rich A (ratio 1.31) p90 | rich B (ratio 1.26) p90 | poor control (ratio 0.65) p90 |
|---|---|---|---|
| −32 | 26.12 | 30.29 | 36.63 |
| −16 | 26.36 | 27.98 | 36.88 |
| −4 | 29.31 | 26.89 | 37.10 |
| **0** | **29.15** | **27.51** | **37.17** |
| +4 | 30.20 | 27.27 | 37.23 |
| +16 | 24.85 | 29.44 | 37.49 |
| +32 | **23.47** | 27.03 | 37.71 |
| | 22% range, **argmin at endpoint** | 14% range, 3 troughs | 4% range, **argmin at endpoint** |

Read it: on rich A, deliberately breaking the seam by 32 px makes the score **better** (29.15 →
23.47). `implied_dx` on the same frame wanders over −0.8 … −53 px *across the sweep of a single
frame*, and on the control it sits at −100 … −170 px regardless of what shift was applied. The
objective is not merely noisy — it is uninformative, and in places anti-correlated with the truth.
This reproduces independently on a fourth frame swept by the calibration-UI work.

**Mark's insight — "it's easy to see misregistration if there's a person in the seam" — is correct,
and it is also the reason SCR cannot use it.** A person is vertical structure, which is exactly what
a horizontal shift displaces visibly. But a vertical structure at the seam sits *inside* the blend
window, where it exists only as a superposition of the two sensors' views. There is no left-shoulder
copy and right-shoulder copy to extrapolate and compare, so the extrapolation primitive cannot
consume it at any weighting. Tested rather than argued: selecting the 12 frames richest in
vertical-edge energy at the seam gave `implied_dx` spanning **−55 … +141 px across a fixed camera**,
4 of the 12 yielded **zero** usable observations, and the sweep above is on two of those frames
**[M]**. Frame selection and orientation weighting do not rescue it; they select for the content SCR
must discard.

**What this leaves.**

- SCR remains a fine *reporting* metric for a human (Workflow B shows it live, §11) and a fine
  acceptance check once a correction exists. It is not a solvable objective.
- The information about `dx` is in the **ghosting inside the blend window** — two copies of the world
  at a fixed offset, `(1−α)·W(x) + α·W(x−e)`. Recovering `e` from that is an echo-separation problem,
  not an extrapolation problem, and it is the one fused-frame primitive that can use a person at the
  seam. Unbuilt, and the honest next step for Workflow A.
- Better still, §14 question 1: `VIDEOPROC 2` out port 1 is a live 256×2160 output nobody reads. If
  it carries the two contributions separately, this stops being an inference problem and becomes
  direct two-image registration, where vertical structure is precisely what one correlates. The
  archive result promotes that from "highest-value lead" to "the thing to do next".
- `stitch_solver` therefore ships with `sweep_dx` / `require_responsive_objective`: before any curve
  is trusted, the objective must be shown to have an interior, deep, single-troughed minimum. On this
  footage it does not, and the solver says so.

**One unit trap, worth stating.** Anchors are in the pixel units of the frame they were measured on,
and `build_dx_lookup` rescales them — so a profile solved on a downscaled still is correct, but its
*integer* downstream projection is not free: `StitchProfile` stores `int` dx, so a profile solved at
half resolution carries a ±0.5 px quantisation that becomes ±1.0 px after rescaling. Solve at full
resolution where possible; the camera-mesh surface is unaffected (quarter-pixel, §5.1). **[D]**

---

## 11. Workflow B — human-in-the-loop

Mark's framing: *"get screenshot from camera, roll/move/manipulate both sides until stitching is
correct, feed geometry back to camera over http"*.

**Fetch.** `cmd=Snap&channel=0` over HTTP returns the full 7680×2160 JPEG in ~2 s, no shell **[M]**.
`GetStitch` fetches the current scalars and the factory `initial` block in the same round trip. The
whole of Workflow B up to "apply" therefore needs nothing but HTTP.

**What the operator manipulates.** Gestures, all writing into the same anchor curve:

- **translate** — arrow keys / drag: a constant added to every anchor.
- **roll** — a rotary handle: a linear-in-y ramp, which is the physically expected shape (§2). This
  should be the default and most prominent control.
- **per-row shear** — drag any of 5 anchor handles on a vertical curve widget beside the seam.
- **direct** — grab the seam at a row and slide it; the nearest anchor follows.

The panel always shows the current curve numerically, and it is *the artifact* — there is no separate
export step and no parameters that exist only in the UI.

**Keeping the mental model honest given constraint 1.** The camera can only move the **left** image.
So:

- Show **one** draggable half. The right half is drawn locked and dimmed. Never two draggable halves.
- Persistent caption: *"You are moving the LEFT image relative to the right. The camera can only move
  the left."*
- If the operator selects the downstream surface instead, the UI **flips which half is draggable**
  and inverts the displayed sense, because that is the half that will actually move there
  (§4.4). The stored anchors do not change — only the presentation does. The surface selector
  therefore has a visible consequence, which is the point.

**Visualising misregistration.** The seam must be *obviously* wrong when it is wrong:

1. **Blink** — alternate the two extrapolated shoulders at ~2 Hz. Motion is the most sensitive
   misalignment detector a human has; this should be the default view.
2. **Anaglyph** — left contribution red, right cyan, over the blend window. Registered reads grey.
3. **Strip zoom** — the 256-px window at 4×, full height, as a tall ribbon beside the main view with
   a 1-px grid.
4. **Mirror** — reflect the right shoulder across x = 3840; continuous structure becomes symmetric.
5. **Live numbers** — SCR `p50`/`p90` and `|ln SSR|` (§9) updating as they drag.

That last one is what makes the two workflows genuinely one tool: the human is descending the *same*
objective the automated solver optimises, so their output is directly comparable and directly
mergeable.

**One honesty caveat, stated in the UI.** In a fused JPEG the blend window is already mixed, so modes
1, 2 and 4 operate on *extrapolations from the shoulders*, not on separated layers. They show what
registration implies, not ground truth inside the window. If the pre-stitch snapshot path of §9.1
turns out to be reachable, these modes become exact and the caveat is deleted.

**Transmit.** One "Apply", which does exactly what `correction_owner` says: `SetStitch` over HTTP for
scalars; the §6.2 push plus §7 install for the mesh; nothing at all for downstream. Then it
re-fetches a fresh `Snap` and re-scores, so the operator sees the before/after numbers, not a
promise.

### 11.1 Two corrections to the above, from Mark, 2026-08-17

Both change the shape of the tool, so they are recorded here rather than only in the code.

**The client is a phone, at the pitch side.** *"I will connect to the camera from my phone while on
the field, so setting this up in the camera manager app will work."* An earlier reading of this
section assumed the field ruled out a live view and made the file door primary; that inference was
wrong. The live `Snap` is the primary door and the loop is: tripod → aim → snap → adjust → apply →
re-snap. Consequences, all implemented in `video_grouper/web/stitch_calibration.py`: touch gestures
(axis-locked one-finger drag, pinch zoom, thumb-sized roll and nudge controls) rather than
mouse-and-keyboard; a single-column layout for a 390 px viewport with everything secondary folded
away, because a pair of 640×2160 strips and a tall curve widget do not fit by shrinking; no external
font or CDN request; every score request bounded by a 9 s timeout, with numbers that go visibly
stale the instant the curve moves and a backoff retry that recovers them. Reaching the app from a
phone needs `[TTT] auth_server_bind` set to the machine's LAN address — the host allowlist is
loopback plus that value, and `0.0.0.0` does not widen it **[R]**. A new `POST /stitch/aim` returns
a 1/8-scale panorama (~35 KB) with the seam marked, cheap enough to press repeatedly while someone
walks into position, and it never disturbs a scored session.

**The calibration target is a person standing in the seam.** *"It's easy to see misregistration if
there's a person in the seam."* This is the answer to the anomaly in §9.1: SCR fits **near-horizontal**
structures and `r_y = -m·dx`, so a horizontal edge is *invariant* under the horizontal shift being
tuned. On a sideline mount almost everything crossing the vertical seam runs horizontally — far
touchline, treeline, painted banners — so a frame can produce dozens of observations, pass every
coverage gate, and carry no information at all. Measured **[M]**:

- On three games, of 11, 61 and 88 accepted structures, **0, 1 and 2** were steeper than |m| = 0.15;
  restricting the dx sweep to the subset that can see dx moved p90 by **1.3%, 2.9% and 1.8%** — no
  better than the full set. Re-weighting does not rescue these frames; only different structure does.
- Across 87 frames of the archived set (`F:\archive\duo3_stitch\frames`), whole-band `|ln SSR|`
  cannot distinguish a corridor with upright structure from one without (median **0.095** against
  **0.088**), while the same metric restricted to the rows holding that structure separates them by
  an order of magnitude (**1.889** against **0.170**). 48 of the 87 read below the 0.10 noise floor
  whole-band — "a perfect seam" — and 16 of those have upright structure whose own rows read a median
  of **1.49**.
- On one frame with a player straddling the seam, whole-band `|ln SSR|` is **0.062** and the player's
  rows read **1.180** (2.188 over the strongest band alone).

So the UI makes standing in the seam a numbered **step**, shows where the seam falls before the snap
so the operator can place the person, detects whether anything upright is actually in the corridor
(`vpe/seam_vertical.py`, ~60 ms) and says *put a person in the seam* when nothing is, reports SCR's
steerable subset beside the whole-set number rather than redefining it, and reports `|ln SSR|` over
the target's rows as well as over the field band. Per §12.1 the person stands where play happens, not
beside the tripod, and `calibrated_for.subject_distance_m` records which depth that was.

**The eye is the instrument; the numbers are aids.** The automatic solver (#136) refuses on all 27
archived games for the same geometric reason — median |slope| over 4239 observations is 0.034, so a
10 px error moves the median observation 0.34 px, under its own ~1.3 px noise — and it establishes
the part that bears on this UI: a person standing in the seam is *inside* the blend window, where
the two sensors are already superposed, so there is no separated pair to extrapolate from and no
shoulder-matching estimator can score them (12 vertical-structure-rich frames from one camera gave
`implied_dx` spanning −55..+141 px, 4 of them producing no observations at all) **[M]**. What does
work is a human looking at whether the body is continuous: with ±20 px injected the tearing is
unmistakable at 4× and invisible to the metric **[M]**. The UI is therefore the path that works, not
the fallback, and it says so — every number is presented as secondary to the picture.

**This is a check-and-correct tool, and "nothing to change" is the common answer.** 52 of 96
archived frames sit below the SSR noise floor (#136's sample; 48 of 87 in the independent sample
above, same conclusion) and players straddling the seam look continuous, so it is not established
that these placements are misregistered at all **[M]**. The job is the knocked
tripod, the remount, the new field. The UI states that in the lede, and when `|ln SSR|` is at or
below the noise floor it says so as a verdict in its own right rather than leaving an operator to
hunt for a correction that is not there.

Two honest limits. `|ln SSR|` on the target is raised by the object *existing* inside the window as
well as by misregistration (§9.2 says the same of the 0.3 m live frame), so it is a before/after
comparator on a fixed scene, not an absolute — the UI says so, and the separation figures above are
evidence that the *metric responds to upright structure*, never evidence that those frames are
broken. And note the deliberate difference from §10, which rejects players: that is the automatic
solver accumulating observations across a game where players move and straddle unknown depths. A
cooperating person standing still at a stated distance is the opposite case.

---

## 12. What this does not fix

### 12.1 Parallax — one calibration is correct at one depth

Two lenses separated by a baseline `b` see a subject at distance `d` displaced by

```
disparity_px ≈ f_cyl * b / d
```

`f_cyl = 7680 px / π rad ≈ 2445 px/rad`, from the panorama width and the 180° the renderer assumes
(`render.py:75`, `render_src_hfov_deg = 180.0`) **[R]**. **`b` is unmeasured** — I did not open the
camera and there is no figure for it in the repo **[I]**. With `b = 0.05 m` as an illustration:

| subject distance | disparity | residual after calibrating at 45 m |
|---|---|---|
| 2 m | 61 px | 58 px |
| 8 m (the current `distance` setting) | 15.3 px | 12.6 px |
| 20 m (the `distance` **maximum**) | 6.1 px | 3.4 px |
| 45 m (far touchline midpoint) | 2.7 px | **0** |
| 76 m (far corner) | 1.6 px | 1.1 px |

Read the last column. Calibrating for the far field costs a couple of pixels across the entire far
half of the pitch and tens of pixels at the near touchline. That is the correct trade for this
project — the far ball is 4 px and the near ball is 20–36 px
(`NONUNIFORM_WARP_DESIGN.md`) **[R]** — but it must be a stated choice, which is why
`calibrated_for.subject_distance_m` is a required field.

**`b` is measurable from the tool's own output, and should be.** Two calibrations at two known depths
give two disparities; `f_cyl·b` follows from the difference of `1/d`. Store it as
`calibrated_for.fb_px_m` and the residual at any depth becomes predictable rather than guessed, and
`residual_px_at` can be filled in for real.

Note the interaction with §1.3: the vendor `distance` scalar maxes out at 20.0, one row above the
depth we care about. If that is metres, the coarse surface literally cannot address the far field,
and everything below `camera_mesh` in the precedence chain is a compromise.

### 12.2 Everything else it does not fix

- **The photometric seam.** ~13–33 grey levels between halves **[M]**, from independent per-sensor
  AE. Geometry does nothing for it. The firmware has `adjust_stitch_brightness` and
  `set_ae_stitch_mode` **[M]**; that is a separate investigation and a separate artifact field if it
  ever becomes one.
- **Moving objects at the wrong depth.** A player straddling the seam at 10 m will ghost against a
  45 m calibration no matter how good the calibration is.
- **Vertical misregistration on the downstream path.** `StitchProfile` is horizontal-only by
  construction (`stitch_remap.py:141-143`) **[R]**. §4.2 makes the drop explicit rather than silent.
- **Archived footage.** The mesh cannot retro-fix a recorded game; only the downstream surface can,
  with the §9.2 ceiling on what post-fusion correction can achieve.
- **The 42% problem.** Seam quality sits on top of the worst detection case, but it does not change
  that the far ball is 4 px for optical reasons (`NONUNIFORM_WARP_DESIGN.md`) **[R]**. This is a
  correctness fix, not a resolution fix.

---

## 13. Defects found in existing code while designing this

Not fixed here — this branch is design only — but they are real, reproduced, and should be tracked.

1. **`np.roll` wraparound corrupts the seam.** `stitch_remap.py:143` and `:116`, `:129`. For
   `dx > 0`, far-right-edge content is deposited into the seam columns; for `dx < 0`, seam content is
   deposited at the far right edge. Demonstrated against the shipped function **[M]** (§5.1). Fix:
   shift with edge replication or an explicit fill, not `np.roll`. The current test fixture uses only
   negative `dx` (`tests/test_stitch_remap.py:23`) **[M]**, which is why this has not surfaced.
2. **`stitch_correct` re-encodes 7680×2160 with no rate control.** `stitch_correct.py:59-63` leaves
   `bit_rate` unset **[M]**. It is the first step of the homegrown preset (`presets.py:49-55`)
   **[R]**, so every downstream stage consumes a transcode whose quality nobody chose.
3. **Chroma quantisation at the seam.** `stitch_remap.py:126` forces the chroma shift even, so luma
   and chroma can disagree by 1 px **[R]** — a colour fringe on small objects at the seam.
4. **No `dy` in the profile.** §12.2.
5. **The mesh `SET` path has still never run on hardware** (`lut2d_ioctl.c` header, `../vpe/README.md`)
   **[R]**. First on-camera action must be the identity write: dump the factory mesh, write it back
   unchanged, confirm byte-identical read-back and an unchanged image. Only then write a computed
   mesh.

---

## 14. Open questions, in priority order

1. **What is `VIDEOPROC 2` out port 1 (256×2160), and can it be pulled?** Produced continuously,
   never consumed **[M]**. If it exposes the two contributions separately it changes Workflow A from
   inference to direct registration (§9.1).
2. **Can `stitch_ori_snap` / `stitch snap yuv` be triggered?** Same prize.
3. **Is VPE 1 really unmeshed?** The whole "left half only" constraint rests on it. One procfs query
   settles it; it needs a write, so it was out of scope here (§1.4).
4. **Is `distance` in metres?** Determines whether §12.1's reading of the 20.0 cap is right.
5. **Does the DCE resample chroma correctly at fractional offsets?** (§5.3.)
6. **What does real `dx(y)` look like?** ~~No real profile exists on disk~~ — **still open, and now
   known to be harder than expected.** 27 archived games could not answer it: the seam observations
   they contain do not determine `dx` at all (§10.1, `r²` ≈ 0 on every game), so the roll model of §2
   remains unmeasured on this unit. Answering it needs the deliberate target of §10 step 4, or the
   pre-stitch source images of question 1 — which would turn this from an extrapolation problem into
   a direct registration one and is now the highest-value lead in the document for this reason too.
7. ~~**Is there anything to correct on outdoor footage at all?**~~ **Answered, mostly "no" — §15.**
   52 of 96 archived frames sat below the SSR noise floor **[M]**; §15 adds an independent instrument
   that says the corridor at large carries no ghost on 39 of 40 placements, and explains why.

---

## 15. Auto-measure: what works, what does not, and the evidence for both

Attempted 2026-08-19. **Outcome: the mechanism is established and shipped as an
instrument (`vpe/seam_echo.py`); the fully automatic path is not yet reliable and
therefore refuses rather than publishing.** The goal was an operator "Auto"
button that reads `dx` off whatever happens to be standing in the seam, with no
detector for any object class.

### 15.1 Grey-level echo estimation fails its null — and the instrument is sound

Inside the blend `s(x) = a·b(x) + (1−a)·b(x−d)` for whatever `b` is there, so a
2-tap echo has an autocorrelation peak at lag `d` of height
`a(1−a)/(a²+(1−a)²)` — 0.5 at `a`=0.5, independent of `b`. Estimator: horizontal
gradient → per-row normalised autocorrelation → row-average → median-detrend in
lag → peak.

Injecting a synthetic ghost (`a`=0.5, `d`=18) into **real grass from these very
frames** recovers lag 18 in 6/6 row bands at 0.23–0.57, against clean-control
0.03–0.09. The instrument is not the problem.

On real footage it is nonetheless flat:

- Frame 1104 (`heat__2026.06.06_vs_Fairport_away`, ball at x=3846 visibly two
  copies, required `d` = 17–19): full-height band×lag map at the seam is
  statistically identical to controls at ±400 px; peaks 0.03–0.06, scattered
  lags. A tight grey-level window **on the doubled ball itself** reads
  0.029–0.058 against controls at 0.049–0.076.
- 40 archived frames, many placements, gated by the calibrated
  `vertical_structure`: seam median +0.050 / p90 +0.095 against control median
  +0.039 / p90 +0.074 — **seam median ÷ control p90 = 0.68**, i.e. the seam sits
  *below* its own controls. The single 10× outlier was checked by eye and is
  **two players standing 16 px apart**, both sharp — the same "measured the
  wrong thing" failure as the four detector passes.

**Why:** in grey level a window on the seam is ~33 px of ghosted ball against
~60 px of high-gradient **unghosted** grass, and the grass wins. Averaging more
rows and frames averages more unghosted pixels, so scale does not rescue it.

### 15.2 Colour changes the answer

Grass has enormous luminance texture and almost no colour distinctiveness, which
is why every gradient-energy gate let it through. Measured on the hand-verified
frames, **chroma** distance (Lab a,b — L deliberately excluded, since mown grass
*is* a luminance texture) from the local pitch colour:

| | target | same-row grass | worst foreground grass |
|---|---|---|---|
| chroma distance p95 | 18.8–27.4 | 1.4–2.8 | 2.8–3.2 |
| ratio to target | — | **9.7–13.3×** | **6.6–8.7×** |

Keeping L in the distance collapses those ratios to 3.9× and 1.35×. In the
chroma channel the grass falls to ~0 and the object's two copies are the only
signal left. The ghost is then plainly legible: frame 1104's profile shows two
lobes, ~45 at −14 px and ~33 at +3 px with a dip between, i.e. **separation ~17
px at an amplitude ratio near 0.45** — matching the hand-verified 17–19 px and
`a` = 0.451. A two-copy fit on those rows returns **d = 18.0** with the two-copy
model halving the residual against one lobe (gain 1.99), while the same fit on
frame 1088's ball (106 px out), frame 1120's ball, and four control corridors
gains only 1.06–1.19.

**`a` is predicted, not fitted.** `a(x) = 0.5 − (x − seam)/(2·blend_w)`, so a
ball 106 px out sits at `a_pred` = 0.086 — an 8.6% ghost, which nothing should
be believed on. (An earlier two-copy fit reported "separation 16.1 px" there,
where the truth is 0.) Confining candidates to `a_pred` ∈ [0.29, 0.71] makes
that a geometric refusal rather than a statistical one.

### 15.3 What is still not reliable — the automatic path

Candidate selection ranks by chroma and, on frame 1104, prefers a **walking
person** over the ball. Its bands vote `d` = 21, 24, 28, 31, 32, 33, 34, 35,
several pinned near the search-grid edge. A loose agreement gate published their
median, **25.5 px, where the hand-verified answer is 17–19**. That is the single
most important negative result here and the reason `MAX_SPREAD` is tight.

The disagreement is physical, not noise. The factory mesh nulls the **ground
plane** — grass, painted lines and feet are registered, which is what §15.1 and
the SSR noise floor both say — so residual disparity scales with **height above
the ground**. A walking figure is therefore not one measurement: boots, shorts
and head sit at different heights and legitimately disagree. Two consequences:

1. **Band agreement is not guaranteed even for a genuine target**, so it cannot
   be used as the sole discriminator without also constraining target height.
2. A per-row `dx(y)` shear cannot represent the residual anyway — at one row the
   ground and a person's head need different `dx`. That is stronger than §12's
   list of things the mesh does not fix.

### 15.4 What would make it reliable

- **A single-height target**: a board, a sign, a cone — something flat and
  vertical whose whole extent sits at one height above the ground. The remedy
  text says so. A person is the wrong calibration target for this measurement
  even though they are the right one for the human-in-the-loop workflow.
- **Widen and refine the `d` search** (fits pinning at `D_MAX` = 36 are not
  measurements) and estimate `d` per row band rather than pooling.
- **§14 question 1 remains the clean lead.** `VIDEOPROC 2` out port 1 (256×2160)
  or `stitch_ori_snap` would expose the two contributions *separately*, turning
  this from echo estimation under an unknown mixture into direct registration.

### 15.5 Withdrawn at scale: it measures a step edge, not a ghost

The estimator of 15.2 was run unmodified over the archive and **fails**. It no longer proposes
anchors. This is the fourth withdrawn estimator, so the reasons are recorded specifically enough to
stop a fifth repeating them.

**Acceptance tests, against the shipped module:**

| frame | required | reported |
|---|---|---|
| 1104 (+6 px, real 18 px ghost) | 17-19 px | **33.0 px, published** |
| 1088 (+106 px) | ~0 / refuse | void |
| 1120 (-51 px) | ~0 / refuse | void |

Publishing 33.0 on the one frame that carries a real ghost is worse than refusing.

**The mechanism.** A **step edge** -- a shirt against grass -- is fitted better by two lobes than by
one, so `gain` rises on exactly the objects the chromatic gate is best at finding. `gain` is
therefore *anti-correlated with truth* on this material. On frame 1104 all three accepted candidates
sat on a walking player's torso, shorts and leg, 37-47 px left of the seam; the ball's own row band
ranked **15th** chromatically and `max_fits=10` never fitted it. Forced onto the ball, the fit
returns d=1.

**The null, at scale** (7,688 frames, 46,088 seam candidates, 90,609 controls, 28 games):

| | value |
|---|---|
| seam acceptance | 7.4% |
| control acceptance | 6.3% |
| ratio | **1.18x**, against the module's own `NULL_MARGIN` of 3.0 |
| `ctl-1200` | accepts **more often** than the seam |
| d-histogram overlap | 0.85 |
| seam / control median d | 31 / 30 px -- unchanged at 31 / 30 after matching on colour decile and row band |

96 gate settings were swept; none is both quiet off-seam and correct on it.

**Cross-frame agreement does not rescue it, and inverts.** Within-track IQR is *better at the
controls* (median 1.0 px, 74% <= 1 px) than at the seam (2.0 px, 38%). The steadiest landmark in the
corpus is a **control** reading d = 35.0 px with IQR 0.0 across 25 looks, where the truth is exactly
zero. So agreement cannot be the acceptance criterion.

**And the null as originally written was not a safeguard.** The guard read
`if ctl_ok and seam_rate < NULL_MARGIN * ctl_rate`: on a single frame the controls frequently accept
nothing, `ctl_ok` is empty, and the guard is skipped entirely -- inert at exactly the sample size the
button uses. That is why the single-frame and scale runs disagreed about the same frame. **A null a
small sample can switch off is not a null.**

`HV_sheet_control_top_lab.png` is the picture of it: a page of ordinary single, unghosted players in
*control* corridors, accepted at 19-35 px.

**What a future attempt must do differently.** Colour gating was necessary and insufficient -- it
correctly finds chromatically distinct objects, and distinct objects have step edges. Any fix has to
discriminate a **ghost** from a **step**, which is new work with its own acceptance bar, not a
parameter tweak. Per-candidate shards with every fitted profile are at
`F:/archive/duo3_stitch/harvest/report_shards_dense/` (78 `.npz`), so it can be re-analysed without
re-decoding.

### 15.6 What shipped

`vpe/seam_echo.py` and `POST /stitch/auto`, as **diagnostics only**. `measure()` reports its
candidates, its control corridors and what it would have said (`provisional_dx`), and always returns
`verdict = "withdrawn"`; `anchors_from_measurement` raises unconditionally, so no result however
confident can become a curve. The UI has no adopt control. The chromatic gate, the control machinery
and the plumbing are kept because they are reusable and because the numbers are the evidence.

**The seam is calibrated by hand** (section 11), starting from the camera's installed state
(section 16).

---

## 16. Starting the editor from the camera's current state

Added 2026-08-19. Previously every session began at a flat zero curve, so an operator's first
adjustment was a delta from nothing rather than from what is installed.

### 16.1 The snapshot is already the mesh's output — do not re-warp it

`cmd=Snap` returns the **fused** 7680x2160 panorama: the VPE warp and the stitcher have both run
before the JPEG exists, which is exactly why the blend corridor and its ghost are visible in it
(§15.2). Applying the live mesh to that image would apply the correction **twice**. So the editor
never warps the snapshot. What the mesh can honestly contribute is its own *shape*, which is what
`stitch_apply.seam_profile` extracts.

### 16.2 What is read, and from where

`stitch_apply.read_calibration(host)` — read-only; it dumps the live VPE state to a file on the SD
card and never calls `SetStitch` or `lut2d_ioctl set`:

| source | what it answers |
|---|---|
| `lut2d_ioctl get 0` → `baseline.bin` | the live mesh, and its crc32 |
| `/mnt/sda/stitchcal/factory_boot.bin` | is this unit still at factory? |
| `/mnt/sda/stitchcal/anchors.txt` | the installed calibration — **this is the starting curve** |
| `GetStitch` current vs `initial` | the vendor scalars (already surfaced) |

Both `factory_boot.bin` and `factory_vpe0.bin` exist on the unit and are **byte-identical**
(verified live 2026-08-19, md5 `7ac18ef2988970d09fc4f5a36c6cd311`). `factory_boot.bin` is the name
`S98_StitchCal` writes and documents, so it is tried first.

**Measured on Mark's unit, live, 2026-08-19:** live mesh crc32 `8514014a`, `factory_boot.bin` crc32
`8514014a`, no `anchors.txt`, and the camera's own hook log says
`NOT APPLIED: no /mnt/sda/stitchcal/anchors.txt -- nothing is calibrated on this unit`. The UI says
so in those terms rather than showing a zero curve that looks like a calibration.

### 16.3 The vendor's own solution, per row

The mesh maps destination grid points to **source** pixels and the seam is the left half's last
column, so `src_x - dst_x` there is the horizontal displacement the vendor's optimiser chose for
that row. Measured on the factory mesh:

| | value |
|---|---|
| offset at the seam column | **-586.25 px** (row 0) to **-441.25 px** (mid frame) |
| `s`, source-px per destination-px | 0.6002 .. 0.7168, **0.70018 at mid-row** |

That `s` is an independent cross-check: `compose_correction` documents `s_at_seam` = 0.700 and the
archived compose report measured 0.7002. `s` matters to the operator because the composer writes
`x + dx*s` — a 10 px correction moves source pixels by ~7 px at the seam, not 10.

### 16.4 `dx = 0` **is** factory, so there are two reset buttons and not three

§7.1 is what makes this true: the correction is stored as anchors and composed at **every boot**
against the mesh the firmware just generated. Anchors are therefore always relative to factory, and
an all-zero curve leaves the vendor mesh untouched. "Reset to zero" and "back to factory" were the
same button; there is now one, labelled **Back to factory**, plus **Back to camera current** which
loads `anchors.txt` resampled onto the editor's rows.

### 16.5 The double-compose, and its fix

**Found while building 16.2, fixed and verified live 2026-08-19.**

`apply_calibration` took `wait_for_stable_mesh()` -- the **live** mesh -- as its baseline
unconditionally. `S98_StitchCal` does not: it keeps `applied.sig`, and when the live mesh matches
its own last write it composes from the saved `factory_boot.bin` instead (that is the idempotency
verified on hardware in #135). So on a unit already carrying a calibration the two paths disagreed:

| | mesh |
|---|---|
| immediately after an interactive Apply | `factory (+) old (+) new` |
| after the next reboot | `factory (+) new` |

Same stored anchors, two meshes, differing only by whether the unit had been power-cycled.
`--require-baseline` could not catch it: `format_anchors` stamps the live crc it just measured, so
the interactive compose trivially agreed with itself.

This lands squarely on 16.2 -- "load the installed calibration, nudge it by 2 px, Apply" is exactly
the sequence that doubles.

**The fix mirrors the boot hook rather than inventing a second scheme.** `apply_calibration` now
computes `mesh_signature()` -- `md5` of the table with the 8-byte header skipped, the same byte range
as `S98`'s `dd bs=8 skip=1` -- and if the live mesh matches `applied.sig`, composes from the saved
factory copy. It writes `applied.sig` after a successful set (via `tail -c +9`, because `camsh`
refuses `dd`), so the interactive and boot paths cannot drift apart. The ordering guard now compares
the re-dump against the **live signature seen at decision time** rather than against the baseline
crc: those are the same thing only on an uncalibrated unit, and a baseline comparison would have
refused every legitimate re-calibration while still missing the doubling.

**Verified live end-to-end on the unit, writes included.** Expected crcs were computed host-side
*before* any write, so the doubling was falsifiable rather than assumed:

| step | baseline chosen | resulting mesh crc32 | expected |
|---|---|---|---|
| start | — | `8514014a` | factory |
| apply A (dx = 4 px) | live mesh | **`9604929c`** | `factory (+) A` |
| apply A **again** | saved factory copy | **`9604929c`** unchanged | idempotent (old code: `3cdcb4aa`) |
| apply B (dx = 8 px) | saved factory copy | **`83e3bedc`** | `factory (+) B`, **not** `f18b0999` = `factory (+) A (+) B` |
| restore | — | `8514014a` | factory |

Every write was read-back verified by the camera (`read-back matches: the mesh is live`). The unit
was returned to exactly the state it was found in: all three mesh files back to md5
`7ac18ef2988970d09fc4f5a36c6cd311`, `anchors.txt` / `applied.sig` / `mesh_apply.bin` /
`recheck.bin` removed, and `read_calibration` reporting at-factory and uncalibrated. No reboot, no
flash, no `netstate` change.

---

## Appendix: reproducing the measurements

```sh
# vendor scalars, range, and factory baseline
curl -s -X POST "http://<cam>/cgi-bin/api.cgi?cmd=GetStitch&user=<u>&password=<p>" \
     -H 'Content-Type: application/json' \
     -d '[{"cmd":"GetStitch","action":1,"param":{"channel":0}}]'

# full-resolution still (7680x2160 JPEG, ~470 KB)
curl -s -o snap.jpg "http://<cam>/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=x&user=<u>&password=<p>"
```

Shell reads (port 2323; one command set per connection, stdin to EOF):

```sh
cat /proc/hdal/vprc/info                 # media graph; VIDEOPROC 2 out 1 = blend_w*2 x H
cat /proc/hdal/vendor/vpe/info           # vpe_2dlut_size 257, 4 instances
cat /proc/mtd                            # mtd11 "stitch" = CamStitchPara
cat /mnt/para/stitch.cfg                 # persisted scalars
grep -a -o -E '[ -~]{0,60}blend_w[ -~]{0,60}' /mnt/app/device
echo 48656c6c6f0a | xxd -r -p            # hex transport works; base64 is absent
```

Seam profile from `snap.jpg` (numpy):

```python
band = gray[300:2150]
cm = band.mean(axis=0)  # row-independent = photometric
res = band - cm[None, :]  # structural
E = (np.diff(res, axis=1) ** 2).mean(axis=0)  # detrended gradient energy per column
# fit log E quadratically over x in [3328,3712) u (3968,4352]; SSR = mean(E/fit) over [3712,3968]
```

Measured on the live frame: transition centred x ≈ 3840–3845; derivative energy above 5× background
over x ∈ [3812, 3868]; raw SSR 3.29, detrended 3.19; synthetic-disparity response tabulated in §9.2.
The frame is an indoor IR scene at ~0.3 m against an 8 m stitch setting, so its absolute values are a
mechanism check, **not** a field baseline.
