---
paths:
  - "reolink-firmware-patching/**"
---

# Camera firmware (Reolink Duo 3) — safety rules

A bad pak can brick the only field camera (see `reolink-firmware-patching/docs/BRICK_POSTMORTEM.md`,
the 4917 outage). These rules are hard gates, not advice.

## Before flashing anything
1. **Build only with the scripts in `builds/`.** Never hand-assemble a pak or carry init scripts
   that live outside the repo — the 2026-08 "records at home" incident came from an ad-hoc script.
2. **Both builder gates must pass, and they refuse rather than warn:** the boot chain (`loader`,
   `fdt`, `atf`, `uboot`, `kernel`) byte-identical to stock (`pak_repack.repack()`), and
   `verify/check_recording_default.sh` (S99_NetState present, no init script asserts the override
   flag, home-MAC list non-empty). A builder without the boot-chain assertion (e.g.
   `build_tge_retime.sh`) gets it back-filled before its pak is flashed.
3. **Never patch the `device` binary.** It is the process whose crash bricks the camera; reach the
   encoder/ISP through kernel modules and `/proc` interfaces instead (`docs/ENCODER_ROI_QP.md` §2–4).
4. **Flash with `flash/flash_pak.py`** (local CRC pre-check). Pass `SOCCERCAM_COMMIT` so the manifest
   records the commit (WSL can't read it from a `/mnt/c` worktree).
5. **Record every flashed build in `builds/BUILD_LOG.md`** (base, what changed, hash, what was verified
   on camera). Mark exactly one build CURRENT.

## On the camera
- Use `camsh.py` for shell access; it refuses destructive commands — don't bypass it.
- At home the camera must not record (netstate). The override flag `/mnt/sda/netstate/override` is an
  operator action for a bounded test only: remove it afterwards and confirm the daemon resumed.
  Prefer capturing the live stream (RTSP) over enabling recording at home.
- A claim about camera behavior is verified on the camera (read-back, `venc/info`, a recording
  analyzed), not inferred from the build. Off-camera checks are labeled as such.

## Docs
Findings go in `reolink-firmware-patching/docs/` (dated) and decisions in `training/docs/DECISIONS.md`.
Raw dumps and large captures go to the F: archive, not the repo.
