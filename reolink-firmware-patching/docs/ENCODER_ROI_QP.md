# Encoder ROI / QP bias — where the lever actually is

**Status: analysis complete; nothing run on the camera.** The camera was bricked
and offline for the whole of this work (a faulty `LD_PRELOAD` ioctl shim killed
`device`; recovery is waiting on a UART cable). Claims below come from
decompiling the shipped binaries and from one live `/proc/hdal/venc/info` dump
captured before the brick; each cites its source. Anything that could not be
settled from those is marked **INFERRED** or **UNVERIFIED** in place.

What *is* verified, off-camera:

- `builds/build_roi_qp.sh` runs clean against the stock
  `IPC_NT15NA416MP.4867` pak. The output's CRC matches, `loader`, `fdt`, `atf`,
  `uboot`, `kernel`, `ai` and `app` are byte-identical to the base by SHA-256,
  and unpacking the result confirms the patched `.ko` (same size, new format
  string present, old one gone) plus both installed files.
- The guards fail correctly: a single flipped byte in the `kernel` section trips
  the identity check, and re-running the build over an already-patched pak
  aborts at the byte assertion **without creating an output file**.
- `S37_RoiQp`'s config validator was exercised against 14 fixtures (valid,
  CRLF, disabled, missing key, unknown key, non-integer, shell-injection
  attempt, each range violation, absent file). All 14 behave as specified and
  report a precise reason.

What is **not** verified: everything that requires the camera — whether the
`.ko` loads, whether `/proc/hdal/venc/cmd` accepts a write, whether the ROI
reaches the encoder, and what the coordinates mean. §7 is how to find out.

Why we want this: a bitrate study on real footage found ~50–55% of the encoded
bits going to the treeline and the foreground spectators, and only 8–13% to the
sky despite it being 44% of the frame. The ball and the players are starved. The
encoder can bias bits toward a rectangle. Nothing in the stock firmware ever
asks it to.

`APP_REPLACEMENT_DESIGN.md:216` recorded the lever as "AQ only; ROI/QP-map
present but unexposed → `HD_H26XENC_ROI_WIN` / `USR_QP` driven directly,
per-game". The "unexposed" half is **confirmed** — and by direct observation,
not inference (§1). But the proposed route, driving the HDAL param directly,
is the *worst* of the ones that exist: it is unreachable from outside `device`
(§2) and would otherwise mean patching the one process whose death bricks the
camera. There is a route that touches neither. §4 compares them.

---

## 1. The parameter exists, and userspace never sets it

### Observed on the running camera

The strongest evidence is not static at all. `dumps/probe2/venc_info.txt` is a
live `cat /proc/hdal/venc/info` taken from this unit on 2026-08-15 while it was
encoding. Every per-port table in it has a row for outs 0, 1 and 3. The ROI
table has **no rows at all**:

```
------------------------- VIDEOENC 0  ROI --------------------------------------
out   qp_mode  win  qp  rect(x,y,w,h)
------------------------- VIDEOENC 0  ROW RC -----------------------------------
out   en  i_qp_rng  i_qp_step  i_qp_min  i_qp_max  p_qp_rng ...
0     1   2         1          1         51        4        ...
```

and its neighbours are equally explicit:

| table | out 0 | reading |
|---|---|---|
| `ROI` | *(no rows)* | **no ROI window is in effect on any path** |
| `USER QP` | `en 0  map_addr ........` | QP map off, no buffer ever allocated |
| `SMART ROI` | `en 0  fg_str[..] .....  mode ....` | the codec's own region picker is off too |
| `AQ` | `en 1  i_str 3  p_str 3  max_delta 8  min_delta -8` | on |
| `ROW RC` | `en 1  i_qp_rng 2 … p_qp_rng 4 …` | on |
| `JND` | `en 1  str 7  level 11  threshold 5` | on |

So the lever is present, wired into the driver's own reporting, and **unused**.
That is tier-L confirmation of the design note's "present but unexposed", not an
inference from the existence of a symbol.

Two corrections fall out of the same dump. `APP_REPLACEMENT_DESIGN.md` says bit
allocation today is "AQ only"; it is actually **AQ + Row RC + JND**, all three
enabled, none of them regional. And out 0 is `H265 7680x2160`, `CBR`,
`bitrate 20971520`, `fr 20/1`, with `I(int/min/max) = (35/25/51)` and the same
for P — see §5 for why that QP floor of **25** bounds what an ROI delta can buy.

### Static confirmation, and why no config path exists

`device` statically links the Novatek HDAL userspace library, so the whole
`hd_videoenc_*` layer is inside it.

`device FUN_005c11d0` is the param-id → name map (it feeds
`"HD_VIDEOENC_PARAM_%s: ..."` diagnostics). It gives the complete enum:

| id | name | id | name |
|---|---|---|---|
| 0 | `DEVCOUNT` | 0x10 | `OUT_TRIG_SNAPSHOT` |
| 1 | `SYSCAPS` | 0x11–0x13 | `IN_STAMP_{BUF,IMG,ATTR}` |
| 2 | `PATH_CONFIG` | 0x14 | `IN_MASK_ATTR` |
| 3 | `BUFINFO` | 0x15 | `IN_MOSAIC_ATTR` |
| 4 | `IN` | 0x16 | `IN_PALETTE_TABLE` |
| 5 | `OUT_ENC_PARAM` | 0x18 | `IN_FRC` |
| 6 | `OUT_VUI` | 0x1a | `FUNC_CONFIG` |
| 7 | `OUT_DEBLOCK` | 0x1b | `OUT_ENC_PARAM2` |
| 8 | `OUT_RATE_CONTROL` | 0x1c | `OUT_RATE_CONTROL2` |
| **9** | **`OUT_USR_QP`** | 0x1f | `BS_RING` |
| 0xa | `OUT_SLICE_SPLIT` | 0x20 | `OUT_RATE_CONTROL3` |
| 0xb | `OUT_ENC_GDR` | 0x23 | `OUT_JPEG_ROI` |
| **0xc** | **`OUT_ROI`** | | |
| 0xd | `OUT_ROW_RC` | | |
| 0xe | `OUT_AQ` | | |
| 0xf | `OUT_REQUEST_IFRAME` | | |

`device FUN_005cd150` is `hd_videoenc_set` (it prints `"hd_videoenc_set(%s):\n"`
and passes the literal `"hd_videoenc_set"` to the error reporter). It has a
working `case 0xc:` — the ROI arm is compiled in and functional.

**But nothing calls it with 0xc.** Enumerating every call site into
`FUN_005cd150` and recovering the constant in `w1` (the param argument) by
walking back up to 16 instructions gives 33 sites, and the ids actually driven
are:

```
0x02 PATH_CONFIG   0x04 IN            0x05 OUT_ENC_PARAM   0x0a OUT_SLICE_SPLIT
0x0b OUT_ENC_GDR   0x0f OUT_REQ_IFRM  0x10 OUT_TRIG_SNAP   0x11 IN_STAMP_BUF
0x12 IN_STAMP_IMG  0x13 IN_STAMP_ATTR 0x14 IN_MASK_ATTR    0x15 IN_MOSAIC_ATTR
0x1a FUNC_CONFIG   0x1b OUT_ENC_PARAM2 0x1c OUT_RATE_CTL2  0x20 OUT_RATE_CTL3
```

`0x09 OUT_USR_QP`, `0x0c OUT_ROI`, `0x0d OUT_ROW_RC`, `0x0e OUT_AQ` and
`0x23 OUT_JPEG_ROI` appear at **no** call site. Every one of the 33 sites
resolved to a literal, so there is no variable-param site hiding an ROI call.
*(Caveat: a 16-instruction lookback is a heuristic. It found a constant for all
33, which is the strong form of the result, but it is not a proof by
construction.)*

Corroborating: `netserver`, `cgiserver.cgi` and `router` contain **zero**
occurrences of any `roi`/`qp_map`/`usr_qp` string (byte scan of each binary).
There is no Baichuan message, no CGI command and no config field anywhere in the
Reolink layer that reaches the encoder ROI. **There is no existing config path
to reuse.** The design note's "unexposed" is correct.

The one Reolink-level ROI knob that *does* exist is Smart ROI — `device
FUN_0059cef0` case 0x2f validates `"invalid smart roi enable (%lu), should be
0~1"` / `"invalid smart roi mode (%lu), should be 0~1"` and forwards it as
vendor param `0xf07c`. Smart ROI is the codec's own motion-driven region
picker, not a region we choose, so it is the wrong lever for "always favour this
rectangle".

### What `HD_H26XENC_ROI_WIN` looks like

From the validator `device FUN_005c2df0` case 0xc (which range-checks each
window) plus the `memcpy(dst, src, 0x11c)` and the debug print
`"  [%2d] en(%u) qp(%d) mode(%d) rect(x,y,w,h)=(%u,%u,%u,%u)\n"` in
`hd_videoenc_set` case 0xc:

```c
struct HD_H26XENC_ROI_WIN {        /* 0x11c = 284 bytes */
    uint32_t  unknown0;            /* u32[0] — not validated, not printed   */
    struct {                       /* 28 bytes, 10 of them                  */
        uint32_t enable;           /*   0..1                                */
        uint32_t x, y, w, h;       /*   NOT range-checked at all            */
        uint32_t mode;             /*   0..3                                */
        int32_t  qp;               /*   mode==3: 0..51 ; else -32..31       */
    } win[10];
};
```

284 = 4 + 10 × 28 exactly, and the validated u32 indices are 1+7k … 7+7k for
k = 0…9, which is where the ten-window count comes from. Error strings that
pin the ranges: `"HD_H26XENC_ROI_WIN: enable(%d) is out-of-range(0~1)."`,
`"HD_H26XENC_ROI_WIN: mode(%d) is not supported."`, `"HD_H26XENC_ROI_WIN: when
mode=%u, qp(%d) is out-of-range(0~51)."`, `"HD_H26XENC_ROI_WIN: when mode=%d,
qp(%d) is out-of-range(-32~31)."`.

Note `x/y/w/h` get **no** validation — not against the frame, not against
anything. Whatever a caller passes goes straight through.

---

## 2. Why driving the HDAL param from a helper process does not work

`hd_videoenc_set` does **not** issue an ioctl for ROI. Case 0xc is:

```c
memcpy(cfg_base + port*0xab8 + 0x15c, p_param, 0x11c);   /* userspace shadow  */
FUN_005c4d10(0x111, port, 0xf024, 1);                    /* set a dirty flag  */
```

and `FUN_005c4d10` (`device @ 0x005c4d10`) is nothing but

```c
*(u32 *)(cfg_base + port*0xab8 + (param_id - 0xf000)*4 + 0x3f8) = value;
```

— a write into a dirty-flag array indexed by `vendor_param_id - 0xf000`. The
ROI payload never leaves `device`'s own address space at set time. Compare the
params that *do* go out immediately, e.g. case 4 (`IN`):

```c
arg.path_id  = (0x111 << 16) | (n + 0x7f);   /* 0x0111_0080 for n = 1 */
arg.param_id = 0x1011;  arg.len = 0x10;  arg.ptr = &payload;
ioctl(isf_fd, 0xc0204905, &arg);
```

That `+ 0x7f` is worth pausing on: it independently corroborates the corrected
ISF path-id encoding. Unit `0x0111` is **VideoEnc** — one unit, with every
encode path as a *port* — ports below `0x80` are **out** ports, `0x80..0xff` are
**in** ports, `0xffff` is unit ctrl. `HD_VIDEOENC_PARAM_IN` is an input-side
param, so it lands on `0x01110080` / `0x01110081` / `0x01110083`, while
out-side params such as `PATH_CONFIG` use the bare out index. The live
`PATH & BIND` table (`in 0/1/3/7` ↔ `out 0/1/3/7`) matches.

So the established `/dev/isf_flow0` protocol is confirmed, but there is **no
arithmetic mapping** from HDAL param id to ISF param id — each case expands to
its own hand-written set of ISF ids (`PATH_CONFIG` → `0xf000, 0xf005, 0xf03a,
0xf004`; `IN` → `0x1011, 0x1013`). The vendor ids for the bit-allocation family
are:

| HDAL param | vendor id set by `hd_videoenc_set` |
|---|---|
| 6 `OUT_VUI` | `0xf040` |
| 9 `OUT_USR_QP` | `0xf042` |
| 0xa `OUT_SLICE_SPLIT` | `0xf041` |
| 0xb `OUT_ENC_GDR` | `0xf03f` |
| **0xc `OUT_ROI`** | **`0xf024`** |
| 0xd `OUT_ROW_RC` | `0xf04a` |

A separate process cannot replicate this: the 284-byte payload lives in
`device`'s heap-side shadow and only a dirty flag marks it, so an outside helper
would have to reproduce whatever later flush routine reads that shadow. **Route
"small helper doing the ioctl directly" is not viable without further runtime
RE** — and runtime RE is what bricked the camera last time.

`OUT_USR_QP` (the per-CTU QP map) is worse: `device FUN_005cd150` case 9 copies
16 bytes `{en, ?, map_addr(u64)}` and the validator insists
`"HD_H26XENC_USR_QP: qp_map_addr is NULL."`. It needs a physically-addressable
buffer the encoder can DMA. Not reachable from a shell script. **The ROI window
is the practical lever; the QP map is not.**

---

## 3. The route that is reachable: `/proc/hdal/venc/cmd`

`kflow_videoenc.ko` publishes a debug command node. `isf_vdoenc_proc_init`
(`@ 0x00100ac4`) does `proc_mkdir("hdal/venc")` then `proc_create` for
`info`, `cmd`, `help`, `top`, `dbglevel`, and the module's own help text says:

```
1. 'cat /proc/hdal/venc/info' will show all the videoenc info
2. 'echo xxx > /proc/hdal/venc/cmd' can input command for some debug purpose
   echo vdoenc setfixqp 0 1 26 26 > /proc/hdal/venc/cmd
```

**This is not theoretical — the node exists on this camera.** The live probe run
of 2026-08-15 captured it (`dumps/probe1/02_hdal_tree.txt`):

```
/proc/hdal/venc:
-r-xr-xr-x    1 root     root     0 Aug 15 22:40 cmd
-r-xr-xr-x    1 root     root     0 Aug 15 22:40 dbglevel
-r-xr-xr-x    1 root     root     0 Aug 15 22:40 help
-r-xr-xr-x    1 root     root     0 Aug 15 22:40 info
-r-xr-xr-x    1 root     root     0 Aug 15 22:40 top
```

**UNVERIFIED:** the mode is `0555` — no write bit. A write handler *is*
registered (`isf_vdoenc_proc_cmd_write` is in the symbol table), and root with
`CAP_DAC_OVERRIDE` can open a mode-less procfs file for writing, so `echo >`
from an init script should work. Nobody has actually done it on this unit. The
daemon logs the failure explicitly rather than continuing silently.

`_isf_vdoenc_cmd_vdoenc` (`@ 0x00115820`) walks a 21-entry dispatch table at
`vdoenc @ 0x0012f408` (`{const char *name; void (*fn)(char *); const char
*help;}`, stride 0x18). The relevant rows:

| # | name | handler | help |
|---|---|---|---|
| 07 | `setaq` | `0x0010fc74` | set AQ |
| 12 | `setfixqp` | `0x001119d0` | `PathId Enable IFixQP PFixQP` |
| **13** | **`setroi`** | **`0x00111b44`** | **`PathId RoiIndex Enable QP QPMode Coord_X Coord_Y Width Height`** |
| 14 | `setsmartroi` | `0x00111d40` | `PathId Enable` |

There is **no** `setusrqp` command — another reason the QP map is out of reach.

### The other proc interface (`/proc/kdrv_vdocdc/`) does not reach ROI

`kdrv_h26x.ko` publishes a second, lower-level proc tree —
`kdrv_vdocdc_proc_init` creates `/proc/kdrv_vdocdc/{cmd, version, param,
venc_dbglevel, aq, chn_info, utilization, jpg_*}` — and it does have writable
nodes (`proc_cmd_write`, `proc_param_write`, `proc_aq_write`). None of them
reaches ROI:

| node | what it accepts | regional? |
|---|---|---|
| `cmd` | one verb, `BrcUpdateMode <n>` | no |
| `aq` | `echo [mode] [i str1] [p str1] [i str2] [p str2] > /proc/kdrv_vdocdc/aq` | no — global AQ strength |
| `param` | global codec tunables: `H264/H265RowRCStopFactor`, `RRCNDQPStep`, `RRCNDQPRange`, `RRCSyncRCQPCond`, `ESMVTh`, `MDMode`, `H264YCoefCostTh`, … | no |

Useful adjacent knobs — `aq` in particular is a live, no-flash way to push
global AQ strength up — but none of them lets us name a rectangle.
`/proc/hdal/venc/cmd setroi` remains the only ROI route.

### `NMR_VdoEnc_SetROI` (`kflow_videoenc.ko @ 0x00111a84`)

Disassembly, not decompiler output:

```
00111a94  mov  w19,w0                  ; arg0 = path id (32-bit)
00111a90  mov  x20,x1                  ; arg1 = pointer to ROI info
00111a98  bl   NMR_VdoTrig_GetVidEncoder
00111a9c  cbz  x20,<null path>         ; "[VDOENC][%d] Set ROI Info is NULL"
00111aac  mov  w0,#0xbe8               ; per-path object stride
00111ab8  mov  x2,#0xa8                ; <-- 168-byte struct
00111abc  umaddl x3,w19,w0,x3          ; gNMRVdoEncObj + path*0xbe8
00111ac0  add  x3,x3,#0x388            ;   + 0x388 = the ROI slot
00111ac8  bl   memcpy
00111acc  ldr  x4,[x21, #0x18]         ; encoder object callback
00111ae0  mov  w0,#0xa                 ; cmd 10 = "ROI changed"
00111ae4  blr  x4
```

So: `NMR_VdoEnc_SetROI(u32 path, const void *roi)` copies **168 bytes** into the
per-path encoder object at `+0x388` and pokes the codec callback with command
10. No bounds check on the payload, no scaling, no validation.

### The kernel-side ROI struct (16-byte elements, not 28)

`Cmd_VdoEnc_SetROI` (`@ 0x00111b44`) is the `setroi` handler. Its raw
disassembly pins the layout exactly, because the debug print reads the fields
back with sized loads:

```
00111bf4  ubfiz  x8,x2,#0x4,#0x20      ; x8 = RoiIndex * 16   <-- stride 16
00111c08  ldr    w3,[x19, x8]          ; +0x00  u32  enable
00111bfc  ldrh   w6,[x24, x8]          ; +0x04  u16  x        (x24 = base+4)
00111bf8  ldrh   w7,[x25, x8]          ; +0x06  u16  y
00111c10  ldrh   w8,[x20, x8]          ; +0x08  u16  w
00111c0c  ldrh   w9,[x21, x8]          ; +0x0a  u16  h
00111c04  ldrsb  w4,[x22, x8]          ; +0x0c  s8   qp
00111c00  ldrb   w5,[x23, x8]          ; +0x0d  u8   qp_mode
                                        ; +0x0e  u16  (unused)
```

with `x19 = sp+0x80` the struct base, and the frame zeroing `sp+0x80 … sp+0x127`
covering exactly 0xa8 = 168 bytes — the same 168 the memcpy copies.
168 = 10 × 16 + 8, i.e. **ten windows plus an 8-byte trailer**, matching HDAL's
ten. The kernel packs into 16 bytes what HDAL exposes as 28.

| field | offset | width | range |
|---|---|---|---|
| `enable` | +0x00 | u32 | 0..1 |
| `x` | +0x04 | u16 | *(units unconfirmed — see below)* |
| `y` | +0x06 | u16 | |
| `w` | +0x08 | u16 | |
| `h` | +0x0a | u16 | |
| `qp` | +0x0c | s8 | mode 3: 0..51 absolute; else −32..31 delta |
| `qp_mode` | +0x0d | u8 | 0..3 |
| pad | +0x0e | u16 | |

**Coordinate units: UNVERIFIED.** Nothing in `kflow_videoenc.ko` scales the
rectangle, and the code that programs the hardware
(`h26XEnc_setRoiCfg`, `h26XEnc_setUsrQPMap`, `h26xEnc_setSmartRoiCfg`) is an
**undefined external symbol** in `kdrv_h26x.ko` — it lives in a codec blob we do
not have. u16 fields admit both readings: 7680 needs 16 bits if these are
pixels, and only 7 bits if they are 64×64 CTUs. `kdrv_h26x.ko` logs
`"set ROI[%d/%d] , en = %d, win = (%u, %u, %u, %u), qp = (%d, %u), ret = %d"`
but that string has no resolvable code reference in the module, so it does not
settle it either. §7 gives the experiment that does.

---

## 4. The `setroi` command is broken on stock firmware

`Cmd_VdoEnc_SetROI` parses its nine arguments with

```
00111ba8  add x1,x1,#0x648   -> "%d %d %d %d %d %d %d %d %d"
00111be4  bl  sscanf_s
```

and the nine destination pointers, read straight off the argument registers, are

| arg | register | address | field |
|---|---|---|---|
| PathId | x2 | `sp+0x78` | scratch |
| RoiIndex | x3 | `sp+0x7c` | scratch |
| Enable | x4 | `sp+0x80` | `win[0].enable` (u32) |
| QP | x5 | `sp+0x8c` | `win[0].qp` (**s8**) |
| QPMode | x6 | `sp+0x8d` | `win[0].qp_mode` (**u8**) |
| Coord_X | x7 | `sp+0x84` | `win[0].x` (**u16**) |
| Coord_Y | [sp+0] | `sp+0x86` | `win[0].y` (**u16**) |
| Width | [sp+8] | `sp+0x88` | `win[0].w` (**u16**) |
| Height | [sp+0x10] | `sp+0x8a` | `win[0].h` (**u16**) |

Two consequences fall straight out of that table.

**(a) Only window 0 is ever writable.** Every destination is `win[0]`; `RoiIndex`
scales nothing on the write side (it only indexes the read-back print, via the
`ubfiz` above). And the handler zeroes the entire 168-byte struct before
parsing, so each `setroi` call replaces the whole ROI set with *one* window and
nine disabled ones. One rectangle at a time, by construction.

**(b) `QP` and `QPMode` always come out as 0.** Nine `%d` conversions write nine
4-byte ints into fields that are 1 and 2 bytes wide, so the writes overlap. In
format order the last one is Height at `+0x0a`; its four bytes cover `+0x0a`,
`+0x0b` (the real h) **and `+0x0c`, `+0x0d` — qp and qp_mode**, which receive
the high half of Height and are therefore 0 for any height below 65536.

`kwrap.ko vsscanf_s @ 0x00109b70` confirms the widths: the no-qualifier arm is
`*(int *)dst = value` (a 4-byte store), and separate arms exist for `h`
(`*(short *)`) and `hh` (`*(char *)`), selected at `case 0x68` where the first
`'h'` sets the short flag and a second `'h'` sets the char flag.

So on stock firmware `echo vdoenc setroi 0 0 1 -6 1 0 760 7680 900` enables a
window that requests a QP delta of **zero** — an ROI that reallocates nothing.
The command reaches the encoder; it just can't carry a QP.

### The fix: 26 bytes, in place

Give each conversion the right length qualifier. The replacement is *exactly* the
same length as the original, so no relocation, section size or symbol moves —
this is the smallest possible change to a kernel module:

```
kflow_videoenc.ko  file offset 0x36688  (VMA 0x00136648)
  before: "%d %d %d %d %d %d %d %d %d"     26 bytes
  after:  "%d%d%d%hhd%hhd%hd%hd%hd%hd"     26 bytes
```

The spaces are droppable because numeric scanf conversions skip leading
whitespace: `vsscanf_s` runs the `_ctype` space test (`(_ctype[c] >> 5) & 1`)
before every numeric conversion. The NUL-delimited whole string occurs **exactly
once** in the module (the same 9-int prefix appears inside 13-, 14- and 21-int
format strings, which is why the build script asserts on the NUL-delimited form
and the file offset, not on a bare substring).

`kflow_videoenc.ko` carries **no module signature** (no `~Module signature
appended~` trailer), so a byte patch does not break loading.

### Why this route and not the others

| route | verdict |
|---|---|
| **A. `/proc/hdal/venc/cmd setroi` + 26-byte `.ko` fix** | **Recommended.** Touches one string in `rootfs`; `app` untouched, so it layers on any existing build. `device` is never modified — and `device` dying is what bricked this camera. Worst case (module refuses to load) is no video, which reflashing fixes. Costs: one window only, and it needs a way to write to `/proc`. |
| B. Patch `device` to call `hd_videoenc_set(path, 0xc, …)` | Rejected. There is no existing call to redirect, so it means injecting a code cave that builds a 284-byte struct and calls into HDAL — in the one process whose failure mode is a watchdog reboot loop. Buys 10 windows instead of 1. Not worth it until 1 window is proven insufficient. |
| C. Standalone helper driving `/dev/isf_flow0` | Rejected on evidence: §2 shows ROI is not delivered by an ioctl at set time. Would require reverse-engineering the shadow-config flush first. |
| D. Existing config path (Baichuan / CGI / config file) | **Does not exist.** No ROI string in `netserver`, `cgiserver.cgi` or `router`; no `hd_videoenc_set` call with param 0xc in `device`. |

---

## 5. How much a QP delta can actually buy

From the live `RC` table, out 0 runs `CBR`, `bitrate 20971520`, `fr 20/1`, with
`I(int/min/max) = (35/25/51)` and `P(int/min/max) = (35/25/51)`.

Two consequences for choosing `qp`:

- **The rate controller clamps QP to 25..51.** An ROI delta cannot push the
  region below QP 25 no matter how negative it is. If the stream is operating
  near 30, a −6 delta lands around 24 → clamped to 25, and anything beyond −5
  is wasted. Start small and read the effect rather than reaching for −20.
  **INFERRED:** that the ROI delta is applied before the RC clamp rather than
  after it. Nothing we decompiled shows the order; §7.4 will show it as a
  saturating effect if the clamp wins.
- **AQ is already spending ±8 QP** (`max_delta 8 / min_delta -8` on out 0) on
  its own per-block decisions, and JND is on top of that. The ROI delta composes
  with those, so the *net* QP shift in the window will be smaller than the
  number you set. This is another reason the first prove-out target should be
  "is there a rectangle at all", not "is the rectangle exactly −6".

---

## 6. Per-game configuration

The region moves with the field, so it has to be settable per recording without
reflashing. Same shape as the netstate overrides: a plain file on the writable SD
card, hot-reloaded by a daemon.

```
/etc/init.d/S37_RoiQp              runtime/roiqp/S37_RoiQp          (baked in)
/etc/soccercam_roi.conf.default    runtime/roiqp/roi.conf.example   (baked in)
/mnt/sda/soccercam/roi.conf        the per-game override            (editable)
/mnt/sda/soccercam/roi.log         what was parsed, applied, echoed back
```

Format — flat `key=value`, integers only, `#` comments:

```
enable=1        # 0 = stock bit allocation
path=0          # encode path; 0 = the 7680x2160 main stream
x=0
y=760
w=7680
h=900
qp=-6           # negative = lower QP inside the window = more bits
mode=1          # 3 => qp is absolute 0..51; 0/1/2 => delta -32..31
frame_w=7680
frame_h=2160
```

Validation is whole-file and fail-closed. A line that is not
`^[a-z_]+=-?[0-9]+$`, an unknown key, a missing key, a value outside range, a
window that does not fit inside `frame_w × frame_h`, or a file over 4 KB rejects
the **entire** file with the reason logged. Nothing partial is ever applied. If a
good config had already been applied and the file then goes bad, the daemon
issues an explicit disable so the encoder returns to stock rather than keeping a
stale bias.

The daemon re-applies every 300 s as well as on change, because `device` tears
the encode path down and rebuilds it on any stream-config change (resolution,
bitrate, day/night), which zeroes the ROI struct. **INFERRED** — the teardown is
visible in `device`'s path-config handling, but that the ROI is lost across it
has not been observed.

Every apply logs the kernel's own read-back line
(`[VDOENC][n] Set ROI Index = …, QP = …, QPMode = …`). Against an unpatched
module that line reads `QP = 0, QPMode = 0`, so the §4 bug is visible in the log
rather than silently producing a no-op.

**Open, and honestly unsolved: how the file gets onto the card.** Stock firmware
has no shell (§5d of `FIRMWARE_PATCH_NOTES.md`) and nginx serves `/mnt/sda`
read-only, so today the options are the same three the netstate overrides have —
pull the SD card, use the temporary `tcpsvd` shell from the investigation build,
or bake the default into the pak and reflash. Baking it in is why the format is
one short file: switching fields is four numbers.

---

## 7. Prove-out

Nothing below has been run. In order; stop at the first failure.

### 7.0 Preconditions

Camera recovered, `camera.env` filled in, a root shell on the camera (the
investigation build's `tcpsvd` on 2323, or UART). Lock the exposure first, or
every A/B comparison measures the weather instead of the encoder:

```bash
bash runtime/set_exposure.sh manual 110 20
```

### 7.1 Negative control on the *unpatched* firmware — proves the bug

On stock (or any build without this patch):

```sh
echo vdoenc setroi 0 0 1 -6 1 0 760 7680 900 > /proc/hdal/venc/cmd
dmesg | grep 'Set ROI Index' | tail -1
```

**Expected:** `... Enable = 1, QP = 0, QPMode = 0, Coord_X = 0, Coord_Y = 760,
Width = 7680, Height = 900`. If QP reads back as −6, §4 is wrong, the stock
command already works, and the `.ko` patch is unnecessary — say so and drop it.

### 7.2 Baseline the encoder, and confirm the path index

`/proc/hdal/venc/info` is the best instrument we have here: it is the driver's
own report of the ROI/USER QP/SMART ROI/AQ state, so it answers "did the ROI
land" directly rather than by inference. Take a baseline:

```sh
cat /proc/hdal/venc/info > /mnt/sda/soccercam/venc_before.txt
```

**Expected** (this is what the 2026-08-15 dump already shows, so it is a
regression check rather than a discovery): `out 0` is `H265 7680x2160` in
`PATH CONFIG` / `OUT BS`, and the `ROI` table is empty. Note that reading
`info` also resets the work-status counters ("force reset and wait 1 secnod...")
— harmless, but don't be surprised by it.

If `out 0` is not the 7680×2160 H265 stream on your build, set `path=` in
`roi.conf` to whichever index is.

### 7.3 Flash and confirm the patch took

```bash
bash builds/build_roi_qp.sh \
    IPC_NT15NA416MP.4906_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK_soccercam_comprehensive.pak \
    IPC_NT15NA416MP.4910_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK_roiqp.pak
python flash/flash_pak.py IPC_NT15NA416MP.4910_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK_roiqp.pak
```

then on the camera:

```sh
echo vdoenc setroi 0 0 1 -6 1 0 760 7680 900 > /proc/hdal/venc/cmd
dmesg | grep 'Set ROI Index' | tail -1
cat /proc/hdal/venc/info | sed -n '/ROI ---/,/ROW RC/p'
```

**Expected, two independent readbacks:**

1. dmesg echoes `QP = -6, QPMode = 1` with the rect unchanged. This is the
   decisive check that the 26-byte patch works — on an unpatched module it
   reads `QP = 0, QPMode = 0` (§7.1).
2. The `ROI` table in `/proc/hdal/venc/info`, empty in the baseline, now has a
   row for out 0 giving `qp_mode`, `win`, `qp` and `rect(x,y,w,h)`.

Readback (2) is the one that settles the two things static analysis could not:
whether `NMR_VdoEnc_SetROI`'s write actually reaches the encoder's live state at
all, and **what units the driver believes the rectangle is in** — if the table
reports back `rect(0,760,7680,900)` the driver stores what we sent, and §7.4
then shows where it lands in the picture.

If the write itself fails (`Permission denied`), the `0555` mode question of §3
is the cause.

### 7.4 A/B recordings and the QP heatmap

Three recordings of the same static scene, 60 s each, camera untouched:

| clip | config | purpose |
|---|---|---|
| `A_off.mp4` | `enable=0` | control |
| `B_on.mp4` | `enable=1 qp=-6 mode=1` | the treatment |
| `C_zero.mp4` | `enable=1 qp=0 mode=1` | second control — window present, no bias |

`C` matters: it separates "the ROI window does something" from "enabling a window
does something". A real effect appears in B-vs-A and is **absent** in C-vs-A.

```bash
# fetch via the /downloadfile/ unlock, then:
uv run python reolink-firmware-patching/verify/roi_qp_heatmap.py \
    A_off.mp4 B_on.mp4 --out roi_B_vs_A.png --frames 120 --expect 0,760,7680,900
uv run python reolink-firmware-patching/verify/roi_qp_heatmap.py \
    A_off.mp4 C_zero.mp4 --out roi_C_vs_A.png --frames 120
```

The tool averages the per-64×64-block mean |Laplacian| over N frames in each
clip and divides. Lower QP preserves more high-frequency energy, so a working
ROI is a rectangle of ratio > 1. Read the PNGs.

**Expected:** `roi_B_vs_A.png` shows a band of ratio ≳ 1.05 inside the white
outline and ≲ 1.0 outside it (bits have to come from somewhere — the sky and
foreground should get *worse*, which is the point). `roi_C_vs_A.png` is flat
within noise, and the printed `median` ratio there sets the noise floor that the
B-vs-A effect has to clear.

**This is also the units test.** If the rectangle in `roi_B_vs_A.png` lands under
the white outline, `x/y/w/h` are pixels. If it lands in the top-left at roughly
1/64 the size, they are 64×64 CTUs and every number in `roi.conf` must be
divided by 64. If it lands nowhere, the ROI is not reaching the hardware.

### 7.5 Confirm the bits actually moved

Re-run the band-bits measurement from the original study on `A_off.mp4` and
`B_on.mp4` at identical bitrate settings. **Expected:** the near/far-field bands
gain share and the treeline/foreground bands lose it, with total bitrate
unchanged (the rate controller is still capped). A rise in *total* bitrate
instead of a redistribution means the ROI is bypassing rate control — back the
`qp` delta off.

### 7.6 Soak

Leave a full game recording with `enable=1`. Afterwards check
`/mnt/sda/soccercam/roi.log` for the periodic re-apply and, in particular, for
any gap where the encoder path restarted. Confirm the recording is intact
(`recover_mp4` reports it valid) — an ROI must not cost us the file.

---

## 8. What is still unknown

| question | why it is open |
|---|---|
| Coordinate units (pixels vs CTUs) | The programming code is an external symbol in a codec blob. §7.4 settles it empirically. |
| What `qp_mode` 0, 1 and 2 each mean | HDAL only validates that 3 is absolute and 0/1/2 are deltas. No string or code distinguishes them. |
| `win[0]` byte `+0x0e..0x0f` and the 8-byte trailer at +0xa0 | Zeroed by the handler, never read anywhere we found. |
| `HD_H26XENC_ROI_WIN.unknown0` (u32[0]) | Not validated, not printed, not obviously read. |
| Whether `/proc/hdal/venc/cmd` accepts a write at mode `0555` | Needs the camera. |
| Whether the ROI survives an encode-path restart | Assumed not; the daemon re-applies defensively. |
| Whether the ROI delta is applied before or after the RC QP clamp (25..51) | Decides whether deltas beyond ~−5 do anything. §7.4 shows it as saturation. |
| How the ROI delta composes with AQ (±8) and JND, both enabled | Nothing decompiled orders them. The net shift in the window will be smaller than the configured delta. |

**Resolved by the live `/proc/hdal/venc/info` dump**, previously open: Smart ROI
(`en 0`) and USER QP (`en 0`, no map) are both **off** in stock firmware, so
there is no precedence question to settle with Smart ROI and no QP map already
in play — an ROI window is the only per-region QP state on the encoder.

---

## 9. A correction to the existing notes

`IOCTL_TRACE_FINDINGS.md` states: *"every build in this effort asserts that
`loader`, `fdt`, `atf`, `uboot`, `kernel`, `ai` and (where unchanged) `app` are
byte-identical to the base pak before the CRC is written."*

**That is not true of the committed build scripts.** None of
`build_bitrate_cap.sh`, `build_fps_cap.sh`, `build_soccercam_comprehensive.sh`,
`build_soccercam_v2.sh`, `build_http_unlock.sh` or `build_netstate.sh` contains
any such check, and `pak/pak_repack.py` merely copies unreplaced sections through
without comparing them. The property has held because the repacker only touches
the sections it is handed — which is a reason to believe it, not a guard that
enforces it.

`builds/build_roi_qp.sh` implements the guard properly: a pre-flight assertion
that the swap set does not intersect the boot sections, and a post-build
byte-for-byte SHA-256 comparison of `loader`, `fdt`, `atf`, `uboot`, `kernel`,
`ai` and `app` between base and output, which **deletes the output and exits
non-zero** on any difference. The other builders should be back-filled with the
same check.
