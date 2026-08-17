# Replacing `app/device` on the Duo 3 PoE — design

Status: **design only, nothing built or flashed.** Written 2026-08-16 against
`IPC_NT15NA416MP.4867_2505072124`.

## Why this exists

Four independent binary patches were tried to move the main stream off
7680×2160 so a windowed sensor mode could raise the frame rate. Every one was
verified correct in the binary. Every one had **zero runtime effect** (see
`FIRMWARE_PATCH_NOTES.md` §15). Patching constants is not converging, because
the value reaches the drivers by a path static analysis has not revealed.

The alternative is to stop fighting `device` and replace it — while keeping
everything that is genuinely hard to reproduce.

---

## 1. Keep vs replace

### Keep — and why each is non-negotiable

| Component | Why we keep it |
|---|---|
| `loader`, `fdt`, `atf`, `uboot`, `kernel` | Never modified by any build to date, which is exactly why every bad flash has been recoverable through the web UI. The Duo 3 PoE **UART pinout is uncharacterised**, so there is no out-of-band recovery. Touching the boot chain converts a recoverable mistake into a brick. Also keeps the secure-boot eFuse question moot. |
| All HDAL `.ko` modules | Binary-only, no source. They *are* the SoC support. |
| `nvt_isp.ko` (287 KB), `nvt_ae.ko` (295 KB), `nvt_awb.ko` (204 KB), **`nvt_iq.ko` (708 KB)** | **Image quality lives in the kernel.** ~1.5 MB of vendor-tuned AE/AWB/IQ that `device` only *configures* via `ISP_IOC_*` / `AE_IOC_*` ioctls. A replacement app inherits all of it for free. This single fact is what makes the rewrite tractable — it was the main thing believed to be a blocker and is not. |
| Sensor drivers + `CamEcsPara` | `nvt_sen_os08c10.ko` / `_slave.ko` carry the mode tables; the 31,855-byte `CamEcsPara` shading calibration in the `sp` partition is per-unit factory data. |
| Buildroot/busybox rootfs | Fine as-is. Note busybox has **no `telnetd` applet** but does have `tcpsvd`, `inetd`, `sh`, `nanddump`, `devmem`, `xxd`. |
| **`libbase.so`** | See §2 — this is the surprise, and it is what makes partial replacement viable. |

### Replace

**`device`** (4.9 MB) — the media-pipeline orchestrator. It is the only process
that configures capture → ISP → stitch → encode, and the only thing standing
between us and arbitrary stream geometry.

### Undecided, lean keep

`cgiserver.cgi`, `router`, `netserver`, `recorder`, `onvif`, `rtsp`, `nginx`.
These implement the product's control plane and app/ONVIF/RTSP compatibility.
Keeping them means the camera still behaves like a Reolink camera to the
soccer-cam pipeline, the mobile app, and `flash_pak.py`. Replacing them is a
separate project with no bearing on frame rate or geometry.

---

## 2. The interfaces a replacement must drive

### 2a. The kernel media API (the part that needs RE)

There is **no `libhdal.so` anywhere** in the firmware — verified by searching
both `rootfs_extracted` and `app_extracted`. `device`'s `DT_NEEDED` list is
`libbase.so, libstreambuffer.so, libatomic, libmbed{crypto,tls,x509},
libstdc++, libm, libgcc_s, libc` — no HDAL. So **HDAL is statically linked into
`device`**, and the kernel ioctls are the real, stable API.

Device nodes (confirmed present on the running camera):

```
/dev/isf_vdocap0  /dev/isf_vdoprc0  /dev/isf_vdoenc0  /dev/isf_flow0..7
/dev/nvt_vpe      /dev/nvt_h26x0    /dev/nvtmpp       /dev/nvt_isp
```

ioctl families mapped out of `device` by matching call sites to their error
strings:

| type | subsystem | notes |
|---|---|---|
| `'I'` (0x49) | **ISF flow** — `hd_videocap_*`, `hd_videoproc_*`, `hd_videoenc_*` | 262 call sites, nr 1..33. **This is the main surface to characterise.** |
| `'v'` (0x76) | VPE / DCE geometric warp | Fully worked out. `VPE_IOC_GET_2DLUT = 0xc008760d`, arg = `{u32 id, u32 n}` with the table at byte offset 8; `n` is the mesh dimension. Used to extract the live 257×257 warp mesh, validated against the kernel's own procfs output. |
| `'c'` / `'e'` | ISP / AE (`ISP_IOC_*`, `AE_IOC_*`) | Only needs to be driven enough to hand the kernel its IQ blobs. |

The `'I'` family being ~33 commands is what bounds this work. It is not "dozens
of subsystems"; it is one enum. And the VPE precedent shows the method works
end to end.

### 2b. The inter-daemon bus (the part we do **not** need to reverse)

`libbase.so` exports **540 global functions**, including a complete C++
RPC/message-bus framework:

```
bc_mod_base::register_msg_handle(unsigned int, int(*)(rpc_msg_param_set_t), ...)
bc_mod_base::get_msg_handle / find_msg_handle / unregister_msg_handle
bc_module_route::send_msg_async(rpc_addr_t, ...)
bc_module_route::broadcast_msg(unsigned int, unsigned long, char*, unsigned long)
bc_module_route::register2router()
bc_msg_ctx::call(bc_mod_base*, rpc_msg_param_set_t&)
```

Transport is SysV shared memory plus sockets (`device` imports
`shmget`/`shmat`/`socket`/`bind`; `libbase.so` imports the same set —
consistent with the `/SYSV00000500` and `/SYSV00000504` mappings observed in
`device`'s live memory map).

**Consequence: a replacement `device` links `libbase.so` and speaks the existing
bus natively.** No protocol reversing, no reimplementation, and `cgiserver`,
`router`, `netserver` and `recorder` keep working unchanged.

### 2c. How big is the contract?

All five daemons carry ~635 `MSG_*` names, but that is a **shared enum header**,
not a per-daemon contract. What matters is what `device` actually registers:

- **122 `on_*` handlers** total in `device`
- of which **18** are media-relevant (`enc` / `isp` / `stitch` / `snap` / `md` / `ai` / `image`)

So a stage-2 replacement needs on the order of tens of handlers, not hundreds —
and can be grown incrementally, forwarding or stubbing the rest.

---

## 3. Getting the spec by recording, not guessing

`device` is **dynamically linked**, so `LD_PRELOAD` works. The plan:

1. Build a small aarch64 `.so` that interposes `ioctl()`, logging
   `(timestamp, fd, /proc/self/fd → device node, cmd, decoded dir/type/nr/size,
   and a hexdump of the argument buffer)` to `/mnt/sda/`.
2. Insert it in `rootfs`'s `/etc/init.d/start_app`, which launches the app as
   `cd /mnt/app; ... ./device &` with `LD_LIBRARY_PATH` already pointing at
   `/mnt/app`. A one-line change:
   `LD_PRELOAD=/mnt/app/libiocap.so ./device &`
3. Boot, let the pipeline come up, fetch the log over the already-unlocked
   `/downloadfile/` path.

That trace is the **complete, known-good bring-up sequence** — the exact order
and payloads that take the hardware from reset to a running 7680×2160 stream.

### Turning a trace into an implementation

- **Replay.** Emit the same ioctls, in order, with the same payloads. If the
  pipeline comes up identically, the trace is understood well enough to proceed.
  This is a strong correctness oracle that costs nothing to check.
- **Diff-driven field discovery.** Capture traces under different
  configurations (20 vs 21 fps, different bitrate, sub-stream on/off) and diff
  them. Fields that move with a known input are identified without any
  disassembly. This is how the geometry field carrying 2160 gets found — it is
  the field that differs when the resolution differs.
- **Modify.** Once the geometry field is located, either rewrite it in flight
  (stage 1) or emit corrected values from our own code (stage 2/3).

The trace is worth capturing even if the rewrite never happens: it immediately
answers the frame-rate question that four static patches failed to.

---

## 4. Staged migration

Every stage boots, records, and is revertible on its own.

### Stage 1 — observe, then interpose (`LD_PRELOAD` shim)

Stock `device` does all the work; our `.so` sits in front of `ioctl()`.

- 1a: log only. Zero behaviour change. Produces the spec.
- 1b: rewrite the geometry argument in flight — let `device` ask for 2160 and
  hand the kernel 720.

Delivers the windowed high-fps mode **without replacing anything**, and is
removed by reflashing the previous pak. This is the cheapest path to >30 fps and
should be attempted first.

### Stage 2 — partial replacement

Our own binary owns the media pipeline (capture/ISP/stitch/encode config) and
links `libbase.so` to register the ~18 media-relevant `MSG_*` handlers. Stock
`device` is either not started, or started with its media role disabled while it
continues to serve the remaining ~104 handlers. Requires the `'I'` family to be
understood well enough to bring the pipeline up from scratch — the replay oracle
from stage 1 is what proves that.

### Stage 3 — full replacement

Our binary is the only media process. `cgiserver`/`router`/`netserver`/
`recorder` stay. At this point stream geometry, frame rate, warp LUT and
bitstream handling are all ours.

---

## 5. Recovery

Non-negotiable rules, all already proven in practice:

1. **Only `app` and `rootfs` sections are ever modified.** `pak_repack.py`
   swaps named sections and recomputes the Reolink CRC; every build to date has
   touched only these two. The boot chain is never rebuilt, so the camera always
   comes back to a flashable state.
2. **Every build is CRC-verified locally before upload** (`flash_pak.py` refuses
   to upload a pak whose stored CRC ≠ computed), and the camera verifies again
   before writing.
3. **Known-good paks are kept**: `FACTORY_STOCK_4867.pak` (stock),
   the current daily-driver comprehensive build, and any intermediate that has
   been observed healthy.
4. **Reverting is one command**: `python flash/flash_pak.py <known-good>.pak`.
   Measured: ~48 s upload, camera back in ~55 s.
5. **The filename must match** `IPC_NT15NA416MP.<build>_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK*.pak`
   or the camera rejects it regardless of CRC.

A stage-2/3 build that fails to bring up video is *not* a brick — it is a camera
that answers HTTP with no stream, and `flash_pak.py` fixes it. That property must
be preserved: **never move the replacement app into a position where a failure
prevents the upgrade path from running.** Concretely: keep `nginx`, `cgiserver`
and `upgrade` alive and independent of our binary.

---

## 6. What this buys

| Capability | Status today | After |
|---|---|---|
| Stream geometry | Fixed at 7680×2160; four separate patches failed to move it | Arbitrary — we emit the numbers |
| Frame rate | 21 fps hard ceiling (VTS floor at 2160 rows) | Up to ~63 fps at 720 rows; `fps = 46296/(rows+8)` |
| Warp / stitch | Factory 257×257 mesh from `CamStitchPara` | Our own LUT; already know the format and the `SET` ioctl |
| Bit allocation | AQ only; ROI/QP-map present but unexposed [1] | `HD_H26XENC_ROI_WIN` / `USR_QP` driven directly, per-game |
| Crop | `SetCrop` exists but gated by `videoClip: permit 0` | No ability gate |
| Bitrate | Encoder-validator ceiling ~20.5 Mbps | Our own limits |
| Pre-stitch frames | Unclear | Direct access to per-sensor buffers |

The recurring theme: **every one of these is currently blocked by a policy or
constant inside `device`, not by hardware.**

[1] **Bit allocation no longer needs app replacement — see `ENCODER_ROI_QP.md`.**
"Present but unexposed" is confirmed: `device`'s `hd_videoenc_set` has a working
`OUT_ROI` (0xc) arm that no call site ever invokes, and no Baichuan/CGI/config
field reaches it. But driving the HDAL param "directly" turned out to be the
worst of the available routes — `hd_videoenc_set` case 0xc does not issue an
ioctl at all; it writes a 284-byte userspace shadow and sets a dirty flag, so an
outside process cannot replicate it. The encoder ROI is reachable *without*
touching `device`, through `kflow_videoenc.ko`'s own
`/proc/hdal/venc/cmd setroi` command plus a 26-byte in-place fix to that
command's `sscanf` format string. `USR_QP` remains out of reach from userspace
(it needs a DMA-able QP-map buffer, and has no proc command).

---

## 7. Risk and effort — honest

| Item | Effort | Risk | Notes |
|---|---|---|---|
| Stage 1a — ioctl logger | Hours | **Very low** | Log-only; if the `.so` fails to load, `device` runs normally. Reverts by reflash. |
| Stage 1b — geometry rewrite | 1–2 days | Low | Worst case: no video, reflash. Most likely to deliver >30 fps. |
| `'I'` family characterisation | Days | Low (analysis) | Bounded: ~33 commands, with a replay oracle. |
| Stage 2 — partial replacement | Weeks | Medium | Must not break the upgrade path. |
| Stage 3 — full replacement | Weeks–months | Medium | Diminishing returns unless the camera becomes a product component. |

### What could make this fail

- **Undocumented ordering/timing.** The ISF layer may require sequencing or
  inter-ioctl delays that a naive replay does not reproduce. Mitigation: the
  trace preserves order and timestamps; replay is checked against the live
  pipeline before anything is replaced.
- **Opaque payload structs.** Some ioctl arguments will contain pointers into
  `nvtmpp` buffers or nested structures. Diff-driven discovery handles fields we
  can vary; it does **not** handle fields we cannot influence from outside.
- **Buffer-pool management.** `nvtmpp` allocation is the least-understood part
  and is not exercised by simply reading config. This is the most likely place
  for stage 2 to stall.
- **`device` may not be cleanly separable.** It also serves ~104 non-media
  handlers; if the control plane turns out to be entangled with media state, the
  partial-replacement boundary moves and stage 2 grows.
- **The frame-rate win may be smaller than predicted.** `fps = 46296/(rows+8)`
  is a *sensor* limit. Whether the ISP/stitch/encode path sustains 60 fps at
  7680×720 is unmeasured. Evidence is encouraging (the same path does
  ~330 Mpix/s at 2160 rows, and 7680×720×60 ≈ 331 Mpix/s — right at the same
  budget) but that is arithmetic, not a measurement.

### Unknowns explicitly not resolved here

- Exact semantics of the 33 `'I'` ioctls (that is what the trace is for).
- Whether `nvtmpp` buffer pools can be driven from a non-`device` process.
- Whether the stitch (`VSP`) requires `device`-private state we have not seen.
- `start_app` references `./vg_boot.sh`, `./do_before_app` and `./ropclient`
  which are **absent from the app squashfs** — they fail harmlessly today, but
  it means `start_app` is not a reliable inventory of what runs.

---

## Recommendation

Do **stage 1a immediately** — it is hours of work, near-zero risk, and produces
the artifact that unblocks everything else. Then stage 1b for the frame-rate
result. Only commit to stage 2 once the replay oracle shows the trace is
genuinely understood.
