# Windows firmware patcher

`patch_firmware.py` builds a patched Reolink Duo 3 PoE `.pak` **entirely on
Windows — no WSL**. It's the engine behind the config-UI "Patch camera firmware"
button (Reolink-only). See `../docs/RECORD_GATE_DESIGN.md` for what it patches.

## What it does

From your own stock `.pak` it produces a patched `.pak` with:
- HTTP `/downloadfile/` unlock + bitrate cap (carried), and
- the **record-at-home gate** baked into `start_app` with your home gateway MAC
  (auto-detected from the host, which sits on the home LAN).

Fail-enabled: the camera records unless it positively confirms the home gateway
MAC. The output `.pak` is **UNVERIFIED on a camera** — bench-test per
`../docs/RECORD_GATE_DESIGN.md` before flashing.

## Run from source (Python on Windows)

One-time: install the Windows squashfs tools (7-Zip can read but not *create*
squashfs, so it isn't enough):

```
scoop install squashfs-tools         # provides mksquashfs.exe / unsquashfs.exe
```

Then:

```
python patch_firmware.py stock.pak patched.pak
# or pin the MAC / bitrate explicitly:
python patch_firmware.py stock.pak patched.pak --home-mac aa:bb:cc:dd:ee:ff --kbps 20480
```

## Build the single .exe

The exe bundles `mksquashfs`/`unsquashfs` + the gate template, so end users need
nothing installed. Stage the squashfs binaries first, then PyInstaller:

```
scoop install squashfs-tools
mkdir bin
copy %USERPROFILE%\scoop\apps\squashfs-tools\current\*.exe bin\
copy %USERPROFILE%\scoop\apps\squashfs-tools\current\*.dll bin\   # if any
pyinstaller --noconfirm patch_firmware.spec
# -> dist\patch_firmware.exe
```

CI does exactly this — see `.github/workflows/build-firmware-patcher.yml`
(`workflow_dispatch`, or tag `firmware-patcher-v*` to attach the exe to a
release). `bin/`, `build/`, `dist/` are git-ignored.

## Flashing

Camera web UI → Settings → Maintenance → Local Upgrade → select the patched
`.pak`. Keep your stock `.pak` as the recovery image.
