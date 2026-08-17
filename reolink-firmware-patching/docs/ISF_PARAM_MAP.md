# Novatek ISF ioctl protocol — decode map (Reolink Duo 3 PoE, NT98530)

Written 2026-08-16 against firmware `IPC_NT15NA416MP.4867_2505072124`, HDAL
`00305000:00305001`. Purpose: make the two captured `/dev/isf_flow0` ioctl
traces readable **without another live capture**, and answer the question
`FIRMWARE_PATCH_NOTES.md` §15 ends on — *which call carries the 2160?*

Everything here is static analysis of the shipped `.ko` files plus the already
captured artefacts. **No camera was contacted; the unit is bricked and offline.**

### Confidence tiers

| tier | meaning |
|---|---|
| **S** | confirmed by reading the decompiled driver (function + address given) |
| **L** | confirmed live, from a capture already on disk (`dumps/probe*`, `dumps/trace*.txt`) |
| **I** | inferred — the check that would settle it is stated inline |

### Evidence used

| artefact | what it is |
|---|---|
| `isf_decomp.log` | Ghidra decompile of the ISF ioctl entry, the kflow_common param dispatch, and the kflow_videoprocess param handlers |
| `isf_unitids.log` | decompile of every `*install_id*` / `*id_map*` in the four kflow modules |
| `regunit.log` | decompile of every `isf_reg_unit()` **call site** across 9 kflow modules (produced for this document) |
| `paramfns.log` | decompile of the kflow_videocapture param handlers (produced for this document) |
| `dumps/trace.txt`, `dumps/trace2.txt` | LD_PRELOAD ioctl logs, 231 158 and 440 000 lines, **both parsed whole — nothing sampled** |
| `dumps/probe2/{vcap,vprc,venc}_info.txt` | `/proc/hdal/*/info` snapshots from the same session |

The Ghidra runners live in `C:\Users\markb\Downloads\Reolink_Duo_3_PoE_2505072124\`
(`run_isf_regunit.sh`, `run_isf_paramfns.sh` and their `.py` post-scripts) —
deliberately outside the repo.

---

## 1. The ioctl protocol

All ISF traffic goes through one character device, `/dev/isf_flow0`, handled by
`isf_flow_drv_ioctl` in **kflow_common.ko @ `0010bed0`** (S). The full command
set decoded from that function:

| cmd | `_IOWR` | arg size | driver call | note |
|---|---|---|---|---|
| `0xc00c4901` | `('I',1,12)` | 12 | `isf_unit_set_bind` | |
| `0xc00c4902` | `('I',2,12)` | 12 | `isf_unit_get_bind` | |
| `0xc00c4903` | `('I',3,12)` | 12 | `isf_unit_set_state` | open/start/stop/close |
| `0xc00c4904` | `('I',4,12)` | 12 | `isf_unit_get_state` | |
| **`0xc0204905`** | `('I',5,32)` | 32 | `isf_unit_set_param` **or** `isf_unit_set_struct` | **set-param** |
| **`0xc0204906`** | `('I',6,32)` | 32 | `isf_unit_get_param` **or** `isf_unit_get_struct` | **get-param** |
| `0xc020490a` | `('I',10,32)` | 32 | `isf_unit_release_data` | + 0x128-byte frame descriptor |
| `0xc020490b` | `('I',11,32)` | 32 | `isf_unit_push_data` | + 0x128-byte frame descriptor |
| `0xc020490c` | `('I',12,32)` | 32 | `isf_unit_pull_data` | + 0x128-byte frame descriptor |
| `0xc0084910` | `('I',16,8)` | 8 | `hwclock_get_longcounter` | |
| `0xc0a04911` | `('I',17,160)` | 160 | parse `/hdal-maxpath-cfg` from DTS | returns the per-family max-path caps |
| `0xc4084914/15/16` | `('I',20..22,1032)` | 1032 | `debug_log_wait/output/sig` | |
| `0xc014491f` | `('I',31,20)` | 20 | `isf_init` | |
| `0xc0144920` | `('I',32,20)` | 20 | `isf_exit` | |
| `0xc0144921` | `('I',33,20)` | 20 | `isf_cmd` | |

Anything else falls through to `LAB_0010c324` and returns 0 silently (S).

### 1.1 The 32-byte set-param argument

`isf_flow_drv_ioctl` copies 32 bytes from user into `local_150..local_138`
(`FUN_0010bd30(puVar7,param_4,0x20)` at `0010bed0`+…, S). Word layout:

| u32 | field | notes |
|---|---|---|
| `[0]` | **return slot** | written by the driver (`local_150 = CONCAT44(local_150._4_4_, ret)`) and copied back to user |
| `[1]` | **path_id** | `local_150._4_4_` — see §2 |
| `[2]` | **param_id** | only the low word is used (`local_148 & 0xffffffff`) |
| `[3]` | (unused, part of the `param_id` 8-byte slot) | |
| `[4..5]` | **value** if `len == 0`, else a **userspace pointer** to the payload | passed as one 64-bit `local_140` |
| `[6]` | **payload length** | `(uint)local_138` |
| `[7]` | (padding) | |

Branch, verbatim from the decompilation (S):

```
if ((uint)local_138 == 0)                 /* scalar form  */
    isf_unit_set_param (minor, path_id, param_id, value);
else {
    if (0x1000 < (uint)local_138) return -EINVAL;   /* 4 KiB cap */
    kbuf = __kmalloc(len, GFP_KERNEL);
    copy_from_user(kbuf, userptr, len);
    isf_unit_set_struct(minor, path_id, param_id, kbuf, len);
    kfree(kbuf);
}
```

`0xc0204906` (get) is the mirror image, same 32-byte struct (S):

- `len == 0` → `isf_unit_get_param(minor, path_id, param_id, &out)`, then
  `local_140 = out` — i.e. **the retrieved value lands back in `u32[4..5]` of the
  user buffer**. The trace logs the struct *after* the syscall returns, so for
  every `len == 0` get the trace already contains the answer (S, corroborated L:
  `GET 0x0111 out[0] 0xf038` reads a constant `0x337E9000` — page-aligned, in the
  DRAM range — and never changes across 15 calls).
- `len > 0` → kmalloc, `isf_unit_get_struct(...)`, `copy_to_user(userptr, kbuf, len)`.
  **The payload never appears in the ioctl argument words.** This is the single
  fact that makes the captured traces geometry-blind (see §6).

`minor` is the character-device minor masked to 8 bits (`param_1 & 0xff`) and is
only used for the ownership check in §3.

---

## 2. `path_id` encoding

`isf_unit_set_param` (kflow_common.ko @ **`0010d490`**) and `isf_unit_get_param`
(@ **`0010d5e0`**) split it identically (S):

```
unit_id = (path_id >> 16) & 0xffff
port    =  path_id        & 0xffff
```

### Unit-id validation

```
uVar1 = unit_id - 0x81;
if ((0xfe < uVar1) || ((&DAT_0013a598)[uVar1] == 0))  -> -ENODEV (0xffffffe1)
```

`DAT_0013a598` is the registration table; `isf_reg_unit(int id, void *ctx)`
(kflow_common.ko @ **`0010cff0`**) writes `(&DAT_0013a598)[id - 0x81] = ctx`
after the same `id - 0x81 < 0xff` bound check (S). So **valid unit ids are
0x81 … 0x17f**, 255 slots.

### Port validation and classes

Rejected first, with `-1`:

| port | result |
|---|---|
| `0xfff0`, `0xfff1` | rejected (`0xffef < uVar1 && uVar1 < 0xfff2`) |
| `0xfffe` | rejected |

Then the class test — `((path_id & 0xff80) == 0) || (port - 0x80 < 0x80) || (port == 0xffff)`;
anything else returns `-EACCES`-ish `0xffffffe0` (S):

| port range | class | arrays in the unit ctx (each `[port]`-indexed, 8-byte pointers) |
|---|---|---|
| `0x0000 … 0x007f` | **out[port]** | count `ctx+0x20`; port config `ctx+0x58`; descriptor `ctx+0x30`; bind-type record `ctx+0x38`; allowed-bind mask `ctx+0x48`; ownership record `ctx+0x60` |
| `0x0080 … 0x00ff` | **in[port-0x80]** | count `ctx+0x1c`; port config `ctx+0x50`; descriptor / bind-type record `ctx+0x28`; allowed-bind mask `ctx+0x40` |
| `0xffff` | **ctrl** (unit-level) | — |

Confirmed independently by the error strings in `_isf_vdoprc_do_setparam`
(kflow_videoprocess.ko @ `00185ab0`): `"%s.out[%d] set param[%08x] = %lu"`,
`"%s.in[%d] set param[%08x] = %lu"`, `"%s.ctrl set param[%08x] = %lu"` (S).

### Unit-context vtable (kflow_common's view of a unit)

| offset | contents |
|---|---|
| `+0x08` | unit name string (printed as `%s` in every error message) |
| `+0x18` | a flag consulted by the port-ownership guard |
| `+0x1c` / `+0x20` | in-port count / out-port count |
| `+0x28` … `+0x60` | the per-port pointer arrays tabulated above |
| `+0x88` | (unit-private) device block — VideoProc's `dev` base in §4.3–§4.6 |
| `+0x90` | `_isf_unit_base` ops table; `+0x38` = port-changed notify, `+0xb0`/`+0xb8` = debug print |
| `+0xc0` | `do_setparam` (scalar) |
| `+0xc8` | `do_getparam` (scalar) |
| `+0xd0` | `do_setparamstruct` |
| `+0xd8` | `do_getparamstruct` |

(S — read off `_isf_unit_set_param` @ `00115f20`, `_isf_unit_get_param` @
`00116a00`, `_isf_unit_set_struct_param` @ `001153d4`, `_isf_unit_get_struct_param`
@ `00116360`, and `isf_reg_unit` @ `0010cff0` which installs `_isf_unit_base` at `+0x90`.)

### Ownership guard

On out ports, `_isf_unit_set_param` and `_isf_unit_set_struct_param` both consult
`isf_get_common_cfg()`, and when bit 7 of the result is set (and `minor != 0xff`
and `ctx+0x18 == 0`) compare the port's `*(int *)(record + 0x18)` against
`minor + 0x8000`, refusing with `0xffffffd7` if another `/dev/isf_flowN` minor
owns the port (S). Ghidra aliases the bit-7 operand with the port index in one of
the two functions, so which of the two is tested is ambiguous — irrelevant here,
because everything in both traces runs on `isf_flow0` and passes.

---

## 3. Unit-id table

`install_id` functions are a dead end — they only create VOS semaphores and
flags. The authority is the **`isf_reg_unit()` call sites**, one per module
(`regunit.log`, produced by decompiling every reference to the exported
`isf_reg_unit` symbol):

| unit ids | family | registering function | prefix |
|---|---|---|---|
| `0x81 … 0x89` | **VideoCap 0…8** | `isf_vdocap_drv_init` @ `00153ce0` (kflow_videocapture.ko) | `isf_vdocap0..8` |
| `0x8f` | **VideoOut 0** | `isf_vdoout_drv_init` @ `00100df0` (kflow_videoout.ko) | `isf_vdoout0` |
| `0x91 … 0xa4` | **VideoProc 0…19** | `isf_vdoprc_drv_init` @ `0015edb0` (kflow_videoprocess.ko) | `isf_vdoprc0..19` |
| `0x111` | **VideoEnc** (one unit; every encode path is a **port**) | `isf_vdoenc_drv_init` @ `00101140` (kflow_videoenc.ko) | `isf_vdoenc` |
| `0x131` | **AudioCap 0** | `isf_audcap_drv_init` @ `0010c300` (kflow_audiocap.ko) | `isf_audcap0` |
| `0x133`, `0x134` | **AudioOut 0/1** | `isf_audout_drv_init` @ `00100da0` (kflow_audioout.ko) | `isf_audout0/1` |
| `0x137` | **AudioEnc** | `isf_audenc_drv_init` @ `00100f90` (kflow_audioenc.ko) | `isf_audenc` |

Tier **S**, and the instance index is *not* folded — it is a flat
`base + instance` allocation, one `isf_reg_unit` call per instance, literal
constants in the source. `kflow_ai.ko` and `nvt_gfx.ko` register **no** ISF units
(they are their own `/dev/kflow_ai_net*` / `/proc/nvt_gfx` devices).

`kflow_videodec.ko` and `kflow_audiodec.ko` were not scanned (no traffic in
either trace); by the same pattern their bases are almost certainly in the
`0x1xx`/`0x13x` ranges (**I** — `run_isf_regunit.sh` with those two `.ko` added
settles it in ~40 s, no camera needed).

### The four units the earlier session named — resolved

| unit | name | live corroboration |
|---|---|---|
| `0x0081` | **VideoCap 0** — sensor 0 (`nvt_sen_os08c10_slave`) | `vcap_info.txt`: `VIDEOCAP 0 … bind_dest VIDEOPROC_0_IN_0` (**L**) |
| `0x0083` | **VideoCap 2** — sensor 1 | `vcap_info.txt`: `VIDEOCAP 2 … bind_dest VIDEOPROC_1_IN_0` (**L**) — note it is VideoCap **2**, not 1; the second sensor sits on cap slot 2 |
| `0x0094` | **VideoProc 3** — the sub/ext splitter | `vprc_info.txt`: `VIDEOPROC 3` has out[0..3], bound to `VIDEOENC_0_IN_1` / `VIDEOENC_0_IN_3` (**L**), matching the four out-ports seen on `0x0094` in the trace |
| `0x0111` | **VideoEnc 0** | `venc_info.txt`: `VIDEOENC 0` paths 0/1/3/7 (**L**), matching ports out[0],out[1],out[3],out[7] and in[0],in[1],in[3],in[7] in the trace |

Full mapping for this camera, static ids reconciled against `/proc/hdal` (S+L):

| unit | `/proc/hdal` name | role on the Duo 3 PoE | ports in trace |
|---|---|---|---|
| `0x0081` | VIDEOCAP 0 | sensor 0, 3840×2160 RAW12 @ 20/1 | out[0], ctrl |
| `0x0083` | VIDEOCAP 2 | sensor 1, 3840×2160 RAW12 @ 20/1 | out[0], ctrl |
| `0x0091` | VIDEOPROC 0 | ISP for sensor 0, pipe `RAWALL`, 3840×2160 in → 3840×2160 out | out[0], in[0], ctrl |
| `0x0092` | VIDEOPROC 1 | ISP for sensor 1, identical | out[0], in[0], ctrl |
| `0x0093` | VIDEOPROC 2 | **VSP stitcher**, pipe `VSP`; out[0] 7680×2160, out[1] 256×2160 seam strip | out[0], out[1], in[0], ctrl |
| `0x0094` | VIDEOPROC 3 | pipe `YUVALL`; sub/ext splitter, 4 outs (1536×432, 2560×720, 1280×352, 480×136) | out[0..3], in[0], ctrl |
| `0x0095` | VIDEOPROC 4 | pipe `VPE`, in_max 7680×2160, state `OPEN` (never started) | in[0], ctrl |
| `0x0096` | VIDEOPROC 5 | pipe `VPE`, 1280×352 → 960×352 crop | out[0], in[0], ctrl |
| `0x0111` | VIDEOENC 0 | H.265 main (path 0), H.264 sub (1), H.264 ext (3), JPEG snapshot (7) | out/in 0,1,3,7 + ctrl |
| `0x0131` | AUDIOCAP 0 | | out[0], in[0], ctrl |
| `0x0137` | AUDIOENC | | out[0], in[0] |

The `VIDEOPROC N` ↔ `0x91+N` correspondence is proven three ways: the
`isf_reg_unit(0x91+N, &isf_vdoprcN)` sequence (S); `VIDEOPROC 2` being the only
`VSP` pipe and `0x0093` being the only unit receiving the 632-byte VSP context
struct `0x8000103f` (S+L); and `VIDEOPROC 5` being a `VPE` pipe with `0x0096`
being the only unit receiving the VPE-only `0x8000102f` `pre_scl_crop` (S+L).

> **`/proc/hdal/flow` was never dumped.** kflow_common contains the format
> strings `isf_unit_begin("%s", %08x);` and
> `isf_unit_set_output(ISF_OUT%d, isf_unit_in("%s", ISF_IN%d), %08x);` — that
> file prints the whole graph with each unit's *name next to its id*. It would
> have made this section a one-command lookup. It is item 1 on the capture list
> in §7. The table above stands on its own without it.

---

## 4. Param-id map

Two disjoint spaces, and this distinction is the load-bearing finding of the
whole document:

- **Generic params (`0x0000_1xxx`)** are handled **entirely inside
  kflow_common.ko**. They write the port context and **never reach the unit
  driver**. Grepping kflow_videoprocess for geometry handling finds nothing
  because there is nothing there to find.
- **Unit-private params** — `0x8000_xxxx` for VideoCap/VideoProc,
  `0x0000_fxxx` for VideoEnc, `0x0001_9xxx` for Audio — fall through to the
  unit's own `+0xc0/+0xc8/+0xd0/+0xd8` callbacks.

### 4.1 Generic params — kflow_common.ko (apply to **every** unit)

Scalar path, `_isf_unit_set_param` @ `00115f20` / `_isf_unit_get_param` @ `00116a00` (S):

| id | port class | handler action | name |
|---|---|---|---|
| `0x1001` | out, in | intercepted in kflow_common; validated against the port's allowed-bind mask, then `*(uint*)(bind+4) = value`; debug `"bind-type(%02x=>%02x)"` | **BIND_TYPE** |
| any other | out, in, ctrl | forwarded to `ctx+0xc0` (set) / `ctx+0xc8` (get); debug `"set param(%08x)=%08lx"` | — |

Struct path, `_isf_unit_set_struct_param` @ `001153d4` /
`_isf_unit_get_struct_param` @ `00116360`. All of these are handled *in
kflow_common* and written straight into the port context; the unit driver is
never called (S):

| id | len | port class | port-ctx offsets written | valid-mask bits set | debug string | name |
|---|---|---|---|---|---|---|
| `0x1002` | 4 | out, in, ctrl | ctrl is symmetric (`unit+0x80`). For ports the **set writes port-config `+0x5c` but the get reads a different structure** — descriptor array (`ctx+0x30` out / `ctx+0x28` in) at `+0x30` | — | `set attr(%08x)` | **ATTR** — value must be `< 3` or `== 0xf`, else rejected. The port set/get asymmetry is as-decompiled and unexplained (**S** for both offsets, **I** that it is deliberate) |
| `0x100f` | 8·n | out, in, ctrl | none — **loops** over `len/8` `{u32 id, u32 val}` pairs and calls the unit's scalar `ctx+0xc0` for each | — | `set param(%08x)=%d` | **BATCH_SET** (get: `BATCH_GET`, fills `val` back in place) |
| `0x1010` | 16 | out, in | `+0x10,+0x14,+0x18,+0x1c` | `0x1` | `set vdo-max-frame(%d) vdo-max-size(%d,%d) vdo-max-fmt(%08X)` | **VDO_MAXSIZE** |
| **`0x1011`** | **16** | **out, in** | **`+0x20,+0x24,+0x28,+0x2c`** | `0x70` | `set vdo-size(%d,%d) vdo-format(%08X) vdo-dir(%d)` | **VDO_SIZE** — see §4.2 |
| `0x1012` | 24 | out, in | `+0x30 … +0x44` (6 words) | `0x3000` | `set vdo-winsize(%d,%d,%d,%d) vdo-aspect(%d,%d)` | **VDO_WINSIZE/CROP** |
| `0x1013` | 4 | out, in | `+0x48` | `0x100` | `set vdo-framerate(%d,%d)` — value is packed `(num<<16)|den` | **VDO_FRAMERATE** |
| `0x101f` | 8 | out, in | `+0x08,+0x0c` | `0x2` | `set vdo-src(%d) vdo-func(%08X)` | **VDO_SRC/FUNC** |
| `0x1020` | 16 | out, in | `+0x10 … +0x1c` | `0x1` | `set aud-max-frame(%d) aud-max-bitpersec(%d) aud-max-sndmode(%d) aud-max-samplerate(%d,%d)` | **AUD_MAX** |
| `0x1021` | 12 | out, in | `+0x28,+0x2c,+0x30` | `0x70` | `set aud-bitpersec(%d) aud-sndmode(%d) samplecnt(%d)` | **AUD_FORMAT** |
| `0x1023` | 4 | out, in | `+0x58` | `0x100` | `set aud-samplerate(%d,%d)` | **AUD_SAMPLERATE** |
| any other | — | out, in, ctrl | forwarded to `ctx+0xd0` (set) / `ctx+0xd8` (get) | — | `set param(%08x)=%d` | — |

Note `0x1021`/`0x1023` reuse the same context words as `0x1011`/`0x1013`: the
port context is a union, video for video units and audio for audio units.

`0x1011` alone has a side effect: after writing the four words, kflow_common
follows the port descriptor to its **bound peer** and propagates. Clearest in the
in-port branch of `_isf_unit_set_struct_param` (S):

```
desc  = ((void **)(unit + 0x28))[port];        /* in-port descriptor      */
peer  = *(uint **)(desc + 0x20);               /* the peer's port config  */
if (peer) {
    (*(unit_ops + 0x38))(peer, *(u32 *)(desc + 0x18));   /* port-changed notify */
    if (peer[1] & 0x30) peer[0] |= peer[1] & pctx[0];    /* merge valid-mask bits */
}
```

The out-port branch has the same shape (Ghidra aliased the peer pointer onto
`param_2` there, so the in-port branch is the one to read). This is why an
in-port's geometry tracks whatever the bound out-port was last set to — and it
is the mechanism behind §6.3.

### 4.2 The port context, and why `0x1011` word order is *not* what the label says

Two independent VideoProc consumers read the same offsets, and their **use** is
unambiguous even though Ghidra dropped the varargs of the kflow_common debug
strings:

| port-ctx offset | word idx | consumer proof | field |
|---|---|---|---|
| `+0x20` | `[8]` | `_vdoprc_check_out_dir(unit, port, *(u32*)(pctx+0x20))` — `_vdoprc_update_out` @ `00167734` | **direction** |
| `+0x24` | `[9]` | `_vdoprc_check_out_fmt(unit, port, v)`, and `(v & 0xffffdfff) == 0x51100422` YUV422 test — `_vdoprc_config_out` @ `00166b70` | **pixel format** |
| `+0x28` | `[10]` | printed as the first of `"size(%d,%d) fmt=%08x"`, aligned to the `loff_align` granule into the line-offset field `dev+port*0x38+0x3a4`, stored to `+0x3b8`, zero-checked by `"-out%d:size(%d,%d) is zero?"` | **width** |
| `+0x2c` | `[11]` | printed as the second of `"size(%d,%d)"`, stored to `+0x3bc`, never line-aligned | **height** |
| `+0x38 … +0x44` | `[0xe..0x11]` | `"-out%d:crop(%d,%d,%d,%d)"` | crop x, y, w, h |
| `+0x48` | `[0x12]` | `>>16` / `&0xffff` into `_isf_frc_start`, `"frc(%d,%d)"` | frame-rate control |

Since `_isf_unit_set_struct_param` copies the `0x1011` payload 1:1
(`pctx[8]=p[0]; pctx[9]=p[1]; pctx[10]=p[2]; pctx[11]=p[3]`), the payload is:

```
struct { u32 dir; u32 pxlfmt; u32 width; u32 height; }   /* 16 bytes */
```

**S.** The kflow_common label `"set vdo-size(%d,%d) vdo-format(%08X) vdo-dir(%d)"`
lists the fields in the opposite order; Ghidra recovered no varargs for that
call, so the label ordering is not evidence and the consumer wins. If anyone
wants a belt-and-braces check: `GET (unit, port, 0x1011)` returns the same four
words in the same order (`_isf_unit_get_struct_param` @ `00116360` reads
`+0x20,+0x24,+0x28,+0x2c` into `p[0..3]`), so a single 16-byte get on a port
whose geometry is known from `/proc/hdal` resolves it in one command.

Known pixel-format constants (S, from `_isf_vdocap_do_getportstruct` @ `0015ecb4`
and `_vdoprc_config_out`): `0x41080000` RAW8, `0x410a0000` RAW10, `0x410c0000`
RAW12, `0x410e0000` RAW14, `0x41100000` RAW16, `0x51080400` / `0x51100422`
YUV variants.

### 4.3 VideoProc private params — `_isf_vdoprc_do_setparam` @ `00185ab0` (scalar set)

`lVar4 = ctx+0x88` is the VideoProc device block; all offsets below are relative
to it. Ports are checked against the live out/in counts first (S).

**out[port]** (`port < n_out`):

| param | guard | action | best name |
|---|---|---|---|
| `0x8000000f` | — | accepted, no-op | (reserved/ping) |
| `0x80001025` | `port<0x10` | `dev+0x2810 + port*4 = v` | |
| `0x80001029` | `port<0x10` | `_isf_frc_update_imm(unit, port, dev + port*0x1c + 0x264c, v)`; `v==0` → `0x10001` | **OUT_FRC_IMMEDIATE** |
| `0x8000102d` | `port<0x10` | `dev+0x2f8 + port*4 = v` | (readable via getparam) |
| `0x80001033` | `port<0x10` | `dev+0x2c3d8 + port*4 = v` | |
| `0x80001034` | `port<0x10` | `dev+0x2c474 + port*4 = v` | **LINE_OFFSET_ALIGN** — read back by `_vdoprc_config_out` as `loff_align` and used to align the width |
| `0x80001038` | `port<0x10` | `dev+0x2c41c + port*4 = v` | |
| `0x8000103a` | requires `dev+0x224 bit0` **and** `dev+0x350 bit3` | `dev+0x2bfe0 = 1; dev+0x2bfe4 = v`; else `"USER_CROP_TRIG only support in direct mode!"` | **USER_CROP_TRIG** |
| `0x8000103e` | `port<0x10` | `dev+0x2ca0 + port*4 = v` | |
| `0x80001043` | `port<0x10` | `dev+0x2c4b4 + port*4 = v` | |
| `0x80001111` | `port<0x10` | `dev+0x2c318 + port*8 = v` (**64-bit**) | buffer handle/addr (same array `0x80001032` fills) |
| other | — | `"%s.out[%d] set param[%08x] = %lu"`, `-1` | |

**in[port-0x80]**:

| param | guard | action | best name |
|---|---|---|---|
| `0x8000000f` | — | no-op | |
| `0x8000102a` | `port==0x80` | `_isf_frc_update_imm(unit, port, dev+0x2630, v)`; `v==0` → `0x10001` | **IN_FRC_IMMEDIATE** |
| `0x8000102c` | `port==0x80` | `dev+0x2f4 = v` | (readable via getparam) |
| `0x80001035` | `port==0x80` | `_vdoprc_iport_setqueuecount(unit, v)` | **IN_QUEUE_COUNT** |
| `0x8000103b` | `port==0x80` | `dev+0x19684 = v` | |
| `0x8000103c` | `port==0x80` | `dev+0x19688 = v` | |
| other | — | `"%s.in[%d] set param[%08x] = %lu"`, `-1` | |

**ctrl** (`port == 0xffff`):

| param | action | best name |
|---|---|---|
| `0x80001010` | `dev+0x2f0 = v` | (get returns `dev+0x214` — the pipe id) |
| `0x8000101d` | `dev+0x34c = v` | (also readable via getparam) |
| `0x8000101f` | `dev+0x348 = v` | |
| `0x80001021` | `dev+0x360 = v` | |
| `0x80001022` | `dev+0x364 = v` | |
| `0x80001023` | `dev+0x368 = v` | |
| `0x80001024` | `dev+0x280c = v` | |
| `0x80001026` | `dev+0x36c = v` | |
| `0x80001027` | `dev+0x338 = v` | 3DNR reference port (compared against `port` in `_vdoprc_config_out`'s FRC-conflict check) |
| `0x80001028` | `dev+0x33c = v` | |
| `0x8000102e` | `dev+0x344 = v` | |
| `0x80001037` | `_isf_vdoprc_do_abort` → `ctl_ipp_ioctl(dev+0x48, 6, 0)`; on failure `"DMA abort failed!"` | **ABORT** |
| `0x8000103d` | `dev+0x370 = v` | |
| `0x80001040` | `dev+0x331e8 = v` | |
| `0x8000104a` | `dev+0x3398c = v; dev+0x33988 = 1`; debug `"thermal_info=%d"` | **THERMAL_INFO** |
| other | — | `"%s.ctrl set param[%08x] = %lu"`, `-1` |

### 4.4 VideoProc private params — `_isf_vdoprc_do_getparam` @ `00186330` (scalar get)

| param | port class | returns |
|---|---|---|
| `0x8000102d` | out (`port<0x10`) | `dev+0x26c + port*4` |
| `0x80001010` | ctrl | `dev+0x214` — the **pipe id** (`0x3e` selects the VSP id remap, see `_vdoprc_id_map`) |
| `0x8000101d` | ctrl | `dev+0x34c` |
| `0x8000102c` | in (`port==0x80`) | `dev+0x224` |
| `0x80001110` | in | `ctl_ipp_get_dir_fp(0)` — a **function pointer** for direct-mode access |
| `0x80001112` | in | `FUN_00181574` if `dev+0x228 bit0`, else 0 — another function pointer |
| other | — | `"%s.{out[%d]/in[%d]/ctrl} get param[%08x]"`, `-1` |

### 4.5 VideoProc private params — `_isf_vdoprc_do_setparamstruct` @ `001865d0`

**out[port]**:

| param | len | guard | action | best name |
|---|---|---|---|---|
| `0x8000102f` | 16 | `dev+0x2bffc != 0` (VPE only) and `port<4` | 4×u32 → `dev+0x2c004 + port*0x10` | **PRE_SCL_CROP** (`"pre_scl_crop only for VPE"`) |
| `0x80001030` | 16 | VPE only, `port<4` | 4×u32 → `dev+0x2c044 + port*0x10` | **HOLE_REGION** (`"hole_region only for VPE"`) |
| `0x80001031` | 24 | `port<0x10` | 6×u32 → `dev+0x2c194 + port*24`; words [4],[5] are read back by `_vdoprc_config_out` as the **effective out size** when the feature is enabled | **OUT_REGION / MASK** |
| `0x80001041` | **184** (`0xb8`, size-checked) | — | scatter-copy into `dev+0x2c53c…`, `dev+0x2c6a8…`, `dev+0x2c004…` | **DRE_CONTEXT** (`"dre context size(%lu) not matched(%d)!"`) |
| `0x80001042` | 20 | VPE only, `port<4` | `p[1..4]` → `dev+0x2c084 + port*0x10`; `p[0]` → `dev+0x2c0c4 + port*4` | **CLEAR_WIN** (`"clear win only for VPE"`) |
| `0x80001049` | 12 | `port<0x10` | 3×u32 → `dev+0x2c0d4 + port*12` | |
| other | — | — | `"vdoprc.ctrl set struct[0x%08x]"`, `-1` / `-34` for out-of-range port | |

**ctrl**:

| param | len | action | best name |
|---|---|---|---|
| `0x8000102b` | — | `_isf_vdoprc_oqueue_do_poll_list(unit, buf)` | **OQUEUE_POLL_LIST** |
| `0x80001032` | 192 | 16×u64 → `dev+0x2c318`, then `p[0x20+i]` (16×u32) → `dev+0x2c398` | **OUT_BUFFER_TABLE** (same array `0x80001111` writes one slot of) |
| `0x80001039` | 20 | `dev+0x2c45c = 1`; `p[0..3]` → `dev+0x2c460`; low byte of `p[4]` → `dev+0x2c470` | |
| `0x8000103f` | **632** (`0x278`, size-checked) | `memcpy(dev+0x2c4f8, buf, 0x278)` | **VSP_CONTEXT** (`"vsp context size(%lu) not matched(%d)"`) — the stitcher configuration |
| `0x80001045` | 8 | `dev+0xa8 = *(u64*)buf` | **AI_CALLBACK** (a function pointer; `"ai callback is NULL"` if unset) |
| `0x80001046` | ≥8 | `p[0]` = proc_id (`<4`), `p[1]` = channel; marks `dev + p[0]*8 + 0xb0 = 1`, then `ctl_ipp_set(dev+0x48, 0x1c, …)` | **AI_START** |
| `0x80001047` | ≥8 | same, clears `dev + p[0]*8 + 0xb0`; errors `"path_id %d is not started"` | **AI_STOP** |
| other | — | `"vdoprc.ctrl set struct[0x%08x] = %08lx"` | |

Both AI params first require ≥1 out-port in state 2 (`"vdoprc is not started"`),
a non-NULL `dev+0x48` device handle (`"vdoprc is not opened"`), and reject
`p[0] > 3` with `"proc_id %d > %d"` (S).

### 4.6 VideoProc private params — `_isf_vdoprc_do_getparamstruct` @ `00186f60`

**ctrl only** — the function bails unless `port == 0xffff` (S):

| param | action | best name |
|---|---|---|
| `0x8000102b` | `_isf_vdoprc_oqueue_get_poll_mask(unit, buf)` | **OQUEUE_POLL_MASK** |
| `0x80001036` | writes the constant `0x1000000001` (`{1, 0x10}` as two u32) | **CAPS / VERSION** |
| `0x80001048` | `ctl_ipp_get(dev+0x48, 0x1c, tmp)`; `buf[0] = tmp[0]` | **AI_GET** |
| `0x8000104b` | zeroes `0x44` bytes, then computes from `*(u64*)(in_port_ctx+0x80)` and `*(u16*)(in_port_ctx+0x120) * 1000000` via `_isf_div64`, stores to `dev+0x33990` and `buf[0]` | **MEASURED_RATE** (a line/frame-time readback — this is the one that would report the *actual* achieved rate) |
| other | — | `"vdoprc.ctrl set struct[0x%08x] = %08lx"` |

**Count: 49 distinct VideoProc param ids mapped**, plus 11 generic
kflow_common ids = **60 fully mapped** (handler + context offset + port class).

### 4.7 VideoCap private params (partial) — `_isf_vdocap_do_*` in kflow_videocapture.ko

Not required by the brief, but decoded because units `0x0081`/`0x0083` are where
the sensor rows live. Handlers: `_isf_vdocap_do_setportparam` @ `0015c2b4`,
`_isf_vdocap_do_getportparam` @ `0015d290`, `_isf_vdocap_do_setportstruct` @
`0015d894`, `_isf_vdocap_do_getportstruct` @ `0015ecb4` (S).

| param | op | len | action | best name |
|---|---|---|---|---|
| **`0x8000101a`** | **get** | **≥24** | `buf[0..1] = *(u64*)(ctx+0x20)` (**w, h**); `buf[2] = (fps<<16)\|100` from `ctl_sen_get(id, 5, …)`; `buf[3] = ctx+0x58` (started flag); `buf[4] = ctx+0x30`; `buf[5] = ctx+0x34` | **SEN_CURRENT_DIM_FPS** — the live sensor geometry readback |
| `0x8000101b` | get | ≥16 | `ctl_sen_get(id, 4, …)` → `buf = {w, h, (fps<<16)\|100, pxlfmt}`; in pattern-generator mode (`ctx+0x80 == 2`) returns the hardcoded `{0x0f00, 0x0870, 0x001e0064, 0x41100000}` = **3840, 2160, 30 fps, RAW16**; logs `"sen max dim mode(%d) caps WxH=(%dx%d) fps=0x%X fmt=0x%X"` | **SEN_MAX_CAPS** |
| `0x8000101d` | get | ≥36 | `ctl_sen_get(id, 0x10, …)` → 36-byte plug-info blob | **SEN_PLUG_INFO** |
| `0x80001015` | set | 188 | opens `ctl_sen`, runs `init_cfg`, pinmux, TGE sync, power control; errors include `"sen init_cfg failed"`, `"if type error(0x%X)"`, `"ctl_sen[%d] has been opened!"` | **SEN_OPEN_CFG** — the sensor bring-up struct |
| `0x80001016` | set | 24 | `ctx+0xb4/+0xbc/+0xc4 = p[0..5]`; extra handling when `ctx+0x80 == 2` | |
| `0x80001018` | set | 188 | two scalars only: `ctx+0x124 = p[0]` (default `-0x3334` if 0), `ctx+0x128 = p[4]` (default `0xffffcccc` if 0) | not geometry |
| `0x80001027` | set | 8 | `ctx+0x11fc = *(u64*)p` | |
| `0x8000102d` | set | 12 | `ctx+0x11f0/+0x11f4/+0x11f8` | |
| `0x80001037` | set | — | gyro config; `"Gyro en=%d data_num=%d"`, `"No Gryo driver!"` | **GYRO_CFG** |
| `0x80001038` | set | 52 | opaque blob → `ctx+0x1248 … +0x1278` | |
| `0x8000103d` | set | 4 | `ctx+0x1288 = p[0]` | |
| `0x80001041` | set | — | temperature-sensor TX; `"TSEN TX len[%d] id=%d"` | **TSEN_TX** |
| `0x80001042` | set | — | `"TSEN OOC pa=0x%lX va=0x%lX lofs=%d pack=%d id=%d"` | **TSEN_OOC** |

**13 further ids mapped, 73 in total.** Note that `0x8000101d`, `0x80001027`,
`0x8000102d`, `0x80001038`, `0x8000103d`, `0x80001041` and `0x80001042` collide
numerically with VideoProc ids and mean something entirely different here — the
private param space is **per unit family**, not global. The one gap that matters is which of
`0x80001015` / `0x80001016` / `0x80001038` carries the requested sensor mode
(rows). `0x80001018` and `0x80001038` are ruled out by inspection above;
`0x80001015` is the strong candidate because it is the struct that drives
`ctl_sen` `init_cfg` (**I**). The check that settles it is in §7.

### 4.8 VideoEnc private params — not decoded

The VideoEnc unit's four callbacks are **static functions with no symbols** in
`kflow_videoenc.ko` (only `_isf_vdoenc_do_command` @ `00125190`, the init/uninit
handler, and `_isf_vdoenc_do_input_mask` / `_input_osd` are named). Naming them
means resolving the function pointers written into `isf_vdoenc + 0xc0/0xc8/0xd0/0xd8`
during `isf_vdoenc_drv_init` @ `00101140` — doable statically, not done in this
pass. Four `0x0000_fxxx` ids are nevertheless pinned from the trace against
`venc_info.txt` (**L**):

| param | evidence |
|---|---|
| `0xf005` | always set to `6000`; `venc_info.txt` PATH CONFIG shows `enc_ms 6000` on all four paths → **ENC_TIMEOUT_MS** |
| `0xf038` / `0xf039` | per-port constants `0x337E9000`/`0x058F1B04` (path 0), `0x390ED000`/`0x00935104` (1), `0x39A2D000`/`0x00EA9504` (3), `0x300BF000`/`0x03714304` (7) → **bitstream ring-buffer physical address / size**; the sizes are plausible per-stream (88.9 MiB main, 9.2 MiB sub, 14.7 MiB ext, 55.1 MiB JPEG) and the addresses are page-aligned and monotonically laid out |
| `0xf049` | ctrl; `SET` (8-byte struct) then `GET` (scalar) returning values from `{1,2,3,8,10,11}` — exactly the bit combinations of bits 0,1,3, i.e. **a ready-mask over encode paths 0/1/3**; the `SET` fails with `-1` on 2346/5926 calls → **BITSTREAM_POLL** (set = wait, get = read mask) |

`_isf_vdoenc_do_command` also fixes the encode-path ceiling: `g_vdoenc_path_max_count`
is clamped to `0x20`, and each path context is `0xd90` bytes (S).

---

## 5. Decode of the captured traces

Both files were parsed **in full** — no sampling. `trace.txt` 231 158 lines,
`trace2.txt` 440 000 lines (two PIDs, 755 and 756, so its counts are ~2× the
first file's). `n1`/`n2` below are per-file call counts.

| file | total ioctls | on `/dev/isf_flow0` | `0xc0204905` set-param | `0xc0204906` get-param |
|---|---|---|---|---|
| `trace.txt` | 231 158 | 56 615 | 6 202 | 12 039 |
| `trace2.txt` | 440 000 | 107 592 | 11 824 | 22 908 |

Everything else on `isf_flow0` is frame traffic: `0xc020490c` pull (19 699 /
37 504), `0xc020490a` release (11 702 / 22 295), `0xc020490b` push (6 649 /
12 667). `/dev/nvtmpp` `0xc0185009` (70 147 / 133 623) is the buffer manager.

**Payload rule:** `len` is `u32[6]`. `len == 0` → the value in the `value`
column is the actual scalar (post-call, so for `GET` it is the returned value).
`len > 0` → the payload sat behind a userspace pointer and **is not in the
trace**; only its length is.

### 5.1 VideoCap — `0x0081` (sensor 0) and `0x0083` (sensor 1)

Both units receive an identical sequence; `0x80001028` is the only difference.

| port | op | param | len | n1 | n2 | value / note |
|---|---|---|---|---|---|---|
| out[0] | SET | `0x00001002` | 4 | 1 | 2 | (ptr) — ATTR |
| out[0] | SET | `0x00001011` | 16 | 1 | 2 | **(ptr) — VDO_SIZE** |
| out[0] | SET | `0x00001012` | 24 | 1 | 2 | (ptr) — crop |
| out[0] | SET | `0x00001013` | 4 | 1 | 2 | (ptr) — framerate |
| out[0] | SET | `0x8000101c` | 0 | 1 | 2 | `0` |
| ctrl | GET | `0x8000101a` | 24 | 52 | 98 | **(ptr) — SEN_CURRENT_DIM_FPS, polled** |
| ctrl | SET | `0x80001011` | 0 | 1 | 2 | `0` |
| ctrl | SET | `0x80001014` | 0 | 1 | 2 | `768` (`0x300`) |
| ctrl | SET | `0x80001015` | 188 | 1 | 2 | **(ptr) — SEN_OPEN_CFG** |
| ctrl | SET | `0x80001016` | 24 | 1 | 2 | (ptr) |
| ctrl | SET | `0x80001018` | 188 | 1 | 2 | (ptr) — two scalars only |
| ctrl | SET | `0x8000101d` | 0 | 1 | 2 | `1` |
| ctrl | SET | `0x8000101e` | 0 | 1 | 2 | `0` |
| ctrl | SET | `0x80001020` | 0 | 1 | 2 | `2` |
| ctrl | SET | `0x80001028` | 0 | 1 | 2 | `0` on `0x0081`, **`65536` (`0x10000`) on `0x0083`** — the master/slave distinction |
| ctrl | SET | `0x8000102c` | 0 | 1 | 2 | `65586` = `0x10032` = `(1<<16)\|50` |
| ctrl | SET | `0x80001038` | 52 | 1 | 2 | (ptr) |

### 5.2 VideoProc — `0x0091` … `0x0096`

All six get the same generic-param skeleton; the differences are what identify them.

| unit | ports touched | distinguishing calls |
|---|---|---|
| `0x0091` VIDEOPROC 0 | out[0], in[0], ctrl | the only unit queried with `GET ctrl 0x80001036` (caps), ×1/2 |
| `0x0092` VIDEOPROC 1 | out[0], in[0], ctrl | identical to `0x0091` minus that caps get |
| `0x0093` VIDEOPROC 2 | out[0], **out[1]**, in[0], ctrl | **`SET ctrl 0x8000103f` len 632 — the VSP stitcher context**; `SET in[0] 0x1011` issued **twice** |
| `0x0094` VIDEOPROC 3 | out[0..3], in[0], ctrl | four out ports, each with the full `0x1002/0x100f/0x1010/0x1011/0x1013/0x80001031` set |
| `0x0095` VIDEOPROC 4 | in[0], ctrl only | never configured for output — matches `state OPEN` in `vprc_info.txt` |
| `0x0096` VIDEOPROC 5 | out[0], in[0], ctrl | **`SET out[0] 0x8000102f` len 16 — PRE_SCL_CROP, VPE-only** |

Per-port generic calls, identical shape on every VideoProc out-port
(`n1`/`n2` = 1/2 except where noted):

| op | param | len | note |
|---|---|---|---|
| SET | `0x00001002` | 4 | ATTR |
| SET | `0x0000100f` | 16 | 2 batched `{id,val}` pairs — **contents not in the trace** |
| SET | `0x00001010` | 16 | VDO_MAXSIZE |
| SET | `0x00001011` | 16 | **VDO_SIZE** |
| SET | `0x00001013` | 4 | VDO_FRAMERATE |
| SET | `0x80001031` | 24 | OUT_REGION |

and on every in-port: `0x100f` (8, one pair), `0x1010` (16), `0x1011` (16),
`0x1013` (4). Each unit's ctrl gets `0x1002` (4) and `0x100f` twice (16 and 24 —
2 and 3 batched pairs).

### 5.3 VideoEnc — `0x0111`

Ports out[0], out[1], out[3] receive an **identical** sequence. out[7] — the
JPEG path — differs in four ways: it omits `0xf015` and `0xf042`, issues `0xf018`
once instead of three times, batches `0x100f` as a single 304-byte block instead
of three separate sets, and is never polled with `0xf05b` / `0xf076`:

| op | param | len | n1 | n2 | value / note |
|---|---|---|---|---|---|
| SET | `0x00001002` | 4 | 1 | 2 | ATTR |
| SET | `0x0000100f` | 8, 16, 288 | 3 | 6 | 1, 2 and 36 batched `{id,val}` pairs |
| SET | `0x0000f000` | 0 | 1 | 2 | `2` |
| SET | `0x0000f004` | 80 | 1 | 2 | (ptr) |
| SET | `0x0000f005` | 0 | 1 | 2 | **`6000`** = `enc_ms` |
| SET | `0x0000f015` | 8 | 1 | 2 | (ptr) — absent on out[7] |
| SET | `0x0000f018` | 84 | 3 | 6 | (ptr), issued 3× per port |
| SET | `0x0000f024` | 168 | 1 | 2 | (ptr) |
| SET | `0x0000f03a` | 0 | 1 | 2 | `0` |
| SET | `0x0000f03e` | 288 | 1 | 2 | (ptr) |
| SET | `0x0000f03f` | 24 | 1 | 2 | (ptr) |
| SET | `0x0000f040` | 288 | 1 | 2 | (ptr) |
| SET | `0x0000f041` | 28 | 1 | 2 | (ptr) |
| SET | `0x0000f042` | 32 | 1 | 2 | (ptr) — absent on out[7] |
| SET | `0x0000f04d` | 6 | 1 | 2 | (ptr) |
| SET | `0x0000f069` | 12 | 1 | 2 | (ptr) |
| SET | `0x0000f074` | 12 | 1 | 2 | (ptr) |
| SET | `0x0000f075` | 24 | 1 | 2 | (ptr) |
| SET | `0x0000f07a` | 264 | 1 | 2 | (ptr) |
| GET | `0x0000f038` | 0 | 5 | 10 | bitstream buffer PA (per-port constant, §4.8) |
| GET | `0x0000f039` | 0 | 5 | 10 | bitstream buffer size |
| GET | `0x0000f054` | 52 | 5 | 10 | (ptr) — per-port status block |
| GET | `0x0000f05b` | 0 | 2 | 4 | `0` |
| GET | `0x0000f076` | 0 | 2764 | 5268 | always `0` — the hot poll (~2.8 k / 5.3 k per port) |

In-ports carry **only generic geometry** — there is no private VideoEnc geometry param:

| port | op | param | len | n1 | n2 |
|---|---|---|---|---|---|
| in[0], in[1], in[3] | SET | `0x00001010` | 16 | 1 | 2 |
| in[0], in[1], in[3] | SET | **`0x00001011`** | 16 | **3** | **6** |
| in[0], in[1], in[3] | SET | `0x00001013` | 4 | **5** | **10** |
| in[7] | SET | `0x00001010` / `0x00001011` / `0x00001013` | 16/16/4 | 1/1/2 | 2/2/4 |

ctrl:

| op | param | len | n1 | n2 | value |
|---|---|---|---|---|---|
| SET | `0x0000f049` | 8 | 5926 | 11272 | (ptr) — 2346 / 4482 return `-1` (timeout, no bitstream) |
| GET | `0x0000f049` | 0 | 3580 | 6790 | `1` ×3984, `8` ×2369, `2` ×2345, `10` ×1640, `3` ×24, `11` ×8 — bits 0/1/3 = paths 0/1/3 |

### 5.4 Audio — `0x0131` (AudioCap 0) and `0x0137` (AudioEnc)

| unit | port | op | param | len | n1 | n2 | value |
|---|---|---|---|---|---|---|---|
| `0x0131` | out[0] | SET | `0x00001020` / `0x00019019` / `0x0001901a` | 16 / 36 / 16 | 1 | 2 | (ptr) |
| `0x0131` | in[0] | SET | `0x00001020` / `0x00001021` / `0x00001023` | 16 / 12 / 4 | 1 | 2 | (ptr) |
| `0x0131` | in[0] | SET | `0x00019003` / `0x00019005` / `0x00019018` | 0 | 1/1/2 | 2/2/4 | `0` / `20` / `0` |
| `0x0131` | ctrl | SET | `0x00019001` | 0 | 2 | 4 | `100` then `70` |
| `0x0131` | ctrl | SET | `0x00019010` / `0x00019016` | 0 | 1 | 2 | `1024` |
| `0x0131` | ctrl | SET | `0x0001901f` / `0x00019022` | 0 / 8 | 1 | 2 | `0` / (ptr) |
| `0x0131` | ctrl | GET | `0x00019014` / `0x00019015` | 0 | 1 | 2 | `805814272` (`0x3007C000`, a DRAM PA) / `94720` |
| `0x0137` | out[0] | SET | `0x0000100f` / `0x0000f000` / `0x0000f008` / `0x0000f00d` | 32/0/28/0 | 1 | 2 | — / `1` / (ptr) / `0` |
| `0x0137` | out[0] | GET | `0x0000f00b` / `0x0000f00c` | 0 | 1 | 2 | `805961728` (`0x300A0000`) / `64000` — audio bitstream buffer PA / size |
| `0x0137` | in[0] | SET | `0x0000100f` | 24 | 1 | 2 | (ptr) |

---

## 6. The geometry answer

### 6.1 Nothing in either trace contains a dimension

Both files were scanned for every plausible dimension word — 7680, 3840, 2560,
2160, 1920, 1440, 1152, 1080, 896, 720, 640, 512, 480, 256 — and for every
packed `(w<<16)|h` combination, across all logged `u32=` words of every ioctl on
every device. Results:

| device / cmd | hits |
|---|---|
| `/dev/nvtmpp` `0xc0185009` | `896` ×37/74, `480` ×19/38, `1080` ×1/2 |
| `/dev/nvt_vpe` `0x4008760a/b/d` | `256` ×2/4 each |
| `/dev/kflow_ai_net` `0xc0406604` | `512` ×1/2 |
| **`/dev/isf_flow0`, any cmd** | **zero** |
| packed `(w<<16)\|h`, any device | **zero** |

Those few hits are AI-network tensor sizes and buffer-pool geometry, not stream
geometry. **No stream dimension is present in either trace.** (**L**)

The reason is structural, not accidental: every param that can carry geometry —
`0x1010`, `0x1011`, `0x1012`, `0x1013` — is a struct param, and §1.1 shows that
for `len > 0` the ioctl argument words hold only a *userspace pointer* and a
length. The tracer logged the 32-byte ioctl struct, which is exactly the wrong
32 bytes.

### 6.2 The calls that carry it

Now decodable from §4. All are `cmd = 0xc0204905`, `param_id = 0x00001011`,
`len = 16`, payload `{u32 dir, u32 pxlfmt, u32 width, u32 height}`:

| `path_id` | unit / port | carries (from `/proc/hdal`, **L**) | calls in `trace.txt` |
|---|---|---|---|
| `0x00810000` | VideoCap 0 out[0] | `0 × 0` (VideoCap derives the frame from its IN geometry; `vcap_info.txt` OUT FRAME reads `w 0 h 0`) | 1 |
| `0x00830000` | VideoCap 2 out[0] | `0 × 0` | 1 |
| **`0x00910000`** | **VideoProc 0 out[0]** | **3840 × 2160** | **1** |
| `0x00910080` | VideoProc 0 in[0] | 3840 × 2160 | 1 |
| **`0x00920000`** | **VideoProc 1 out[0]** | **3840 × 2160** | **1** |
| `0x00920080` | VideoProc 1 in[0] | 3840 × 2160 | 1 |
| **`0x00930000`** | **VideoProc 2 out[0]** | **7680 × 2160** — the main stream | **1** |
| `0x00930001` | VideoProc 2 out[1] | 256 × 2160 (seam strip) | 1 |
| `0x00930080` | VideoProc 2 in[0] | 3840 × 2160 | 2 |
| `0x00940000..3` | VideoProc 3 out[0..3] | 1536×432, 2560×720, 1280×352, 480×136 | 1–2 each |
| `0x00960000` | VideoProc 5 out[0] | `0 × 0` in OUT FRAME; the VPE block reports `bg 960×352`, crop `ON:{160,0,960,352}` | 1 |
| **`0x01110080`** | **VideoEnc in[0]** | **7680 × 2160** — encoder input | **3** |
| `0x01110081` | VideoEnc in[1] | 1536 × 432 | 3 |
| `0x01110083` | VideoEnc in[3] | 2560 × 720 | 3 |
| `0x01110087` | VideoEnc in[7] | 7680 × 2160 (JPEG) | 1 |

The corresponding `0x00001010` (`VDO_MAXSIZE`) calls carry the ceilings that
`venc_info.txt` reports as `max_w 7680 max_h 2160` on paths 0 and 7 (**L**).

### 6.3 What this means for the four failed patches

`FIRMWARE_PATCH_NOTES.md` §15 ends with *"the geometry reaches the drivers by a
path static analysis has not revealed."* It is now revealed, and it is not exotic:

```
device
  └─ ioctl(/dev/isf_flow0, 0xc0204905, {path_id, 0x1011, userptr, len=16})
       └─ isf_flow_drv_ioctl            kflow_common.ko @ 0010bed0
            └─ isf_unit_set_struct
                 └─ _isf_unit_set_struct_param   kflow_common.ko @ 001153d4
                      └─ port_ctx[+0x28] = width ;  port_ctx[+0x2c] = height
                                     ↓ consumed later, at state transition
                         _vdoprc_config_out @ 00166b70 / _vdoprc_update_out @ 00167734
                              └─ ctl_ipp_set(...)  →  the IME/scaler hardware
```

Two consequences:

1. **`device` *is* the right lever.** The geometry is sent by `device` over an
   ordinary ioctl. The four earlier patches missed not because the value is
   computed somewhere unreachable, but because they targeted
   `Na_sensor_get_resolution` and the u16 resolution arrays — which the trace
   now shows are reporting/validation paths, not this one.
2. **The `scl_size != in_size` error that blocked the windowed-sensor work is
   `path_id = 0x00910000` (and `0x00920000`), `param = 0x1011`, word[3].**
   `ctl_ipp_int_ime_path_adj() p0 scl_size (3840, 2160) != in_size (3840, 720)`
   fires because the in-port geometry follows the bound VideoCap (which the
   sensor patch moved to 720 rows) while out[0] keeps the 2160 that this call
   wrote. Changing the sensor without changing this call can never bind. (**S**
   for the mechanism, **L** for the error text, **I** for the causal link — §7
   item 3 confirms it.)

### 6.4 The one call to capture

If exactly one payload can be dumped on the return trip:

> **`SET`, `path_id = 0x00910000` (VideoProc 0, out[0]), `param_id = 0x00001011`,
> `len = 16`** — dump the 16 bytes behind the user pointer.

It is issued exactly once per `device` start, early, and its word[3] is the 2160 that
gates the whole windowed-sensor path. Its twin `0x00920000` should be identical.
If two can be dumped, add `path_id = 0x00930000` (VideoProc 2 out[0]) — the
7680×2160 main-stream geometry.

Fabricating the payload from the `/proc/hdal` numbers is **not** safe: the field
order in §4.2 is derived from consumers, and `dir` and `pxlfmt` are unknown for
these specific ports. Dump them.

---

## 7. Capture list for when the camera is back

The camera has been offline for days; the return trip should be one pass, not
three. Everything below is a single command and answers a question this document
had to leave open or infer.

| # | command | resolves |
|---|---|---|
| 1 | `cat /proc/hdal/flow` | The unit-id ↔ name graph in one shot (`isf_unit_begin("%s", %08x)` per unit, `isf_unit_set_output(ISF_OUT%d, isf_unit_in("%s", ISF_IN%d), %08x)` per bind). Confirms §3 outright and gives the two families this pass did not scan. |
| 2 | Re-run the LD_PRELOAD logger **with struct-payload dereferencing** for `cmd == 0xc0204905/06` when `u32[6] != 0` — dump `min(len, 64)` bytes from `u32[4..5]` | Turns every `(ptr)` row in §5 into real data. This is the single highest-value change to the tracer, and §6 exists only because it was missing. |
| 3 | If only a targeted dump is possible: `path_id = 0x00910000, 0x00920000, 0x00930000, 0x01110080`, `param = 0x00001011`, 16 bytes each | The four geometry calls of §6.2/§6.4. |
| 4 | Same, `param = 0x00001010` on the same four path_ids | The max-size ceilings, which must move together with item 3 or the IME will still reject. |
| 5 | Dump the 188-byte payload of `SET path_id=0x0081ffff param=0x80001015` | Settles which VideoCap struct carries the requested sensor mode (§4.7, currently **I**). |
| 6 | Dump the 24-byte result of `GET path_id=0x0081ffff param=0x8000101a` | Live sensor `{w, h, (fps<<16)\|100, started, ?, ?}` — and, incidentally, an independent confirmation of the §4.2 word order. |
| 7 | Dump the 632-byte payload of `SET path_id=0x0093ffff param=0x8000103f` | The VSP stitcher context. Any geometry change to the 7680×2160 output must carry a matching change here, and its layout is entirely unknown. |
| 8 | `cat /proc/hdal/vprc/info /proc/hdal/venc/info /proc/hdal/vcap/info` **before and after** any geometry patch | The only cheap way to tell a bound-and-running pipeline from a silently-rejected one. |
| 9 | `dmesg \| grep -i "ime\|scl_size\|ipp"` after a geometry patch | The IME path-adjust rejection is the failure signature; it is a kernel print, not an API error. |

Item 2 subsumes 3–7 and should be preferred if there is time to modify the
preload shim.

---

## 8. What is still unknown

| gap | why it matters | what settles it |
|---|---|---|
| Every `len > 0` payload in both traces | The actual configuration values, including all geometry | §7 item 2 |
| VideoEnc's four param callbacks are unnamed statics | The `0x0000_fxxx` map in §4.8 is 4 ids out of the 24 observed | Resolve the pointers written to `isf_vdoenc + 0xc0/0xc8/0xd0/0xd8` in `isf_vdoenc_drv_init` @ `00101140` — static, no camera needed |
| The `0x0000_100f` batch contents | 3–36 `{param_id, value}` pairs per unit are invisible; they carry scalar config that never shows as its own ioctl | §7 item 2 |
| Which VideoCap struct sets the sensor mode | The 2160-row source (§4.7) | §7 item 5 |
| `0x8000103f` VSP context layout (632 bytes) | Stitch calibration must move with any geometry change | §7 item 7, then RE against `_vdoprc_*vsp*` in kflow_videoprocess |
| `kflow_videodec` / `kflow_audiodec` unit-id bases | Completeness only; neither appears in the traces | Add both `.ko` to `run_isf_regunit.sh` |
| The `0x1011` field order for `dir` vs `pxlfmt` | Only matters if a payload is ever *synthesised* rather than observed | §7 item 6 |

---

*Cross-reference: `FIRMWARE_PATCH_NOTES.md` §15 (where the 20 fps ceiling is)
and `APP_REPLACEMENT_DESIGN.md` (replacing `device`). This document supplies the
ISF call sequence that a replacement `device` must reproduce.*
