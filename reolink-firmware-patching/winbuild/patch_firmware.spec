# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows-native Reolink firmware patcher.
#
# Produces a single `patch_firmware.exe` that bundles:
#   - patch_firmware.py + the pak/ modules (pak_repack, reolink_crc)
#   - the record-gate template (runtime/recordgate/start_app_gate.template)
#   - mksquashfs/unsquashfs (+ their DLLs), which must be staged into
#     winbuild/bin/ BEFORE building (the CI workflow does this via scoop;
#     locally: `scoop install squashfs-tools` then copy its *.exe/*.dll here).
#
# Build:  pyinstaller reolink-firmware-patching/winbuild/patch_firmware.spec
from pathlib import Path

WINBUILD = Path(SPECPATH).resolve()        # reolink-firmware-patching/winbuild
ROOT = WINBUILD.parent                      # reolink-firmware-patching
BIN = WINBUILD / "bin"

# squashfs binaries land at the bundle root so find_tool() (frozen) sees them.
binaries = [(str(p), ".") for p in BIN.glob("*")] if BIN.is_dir() else []
# gate template keeps its relative path so gate_template_path() (frozen) finds it.
datas = [
    (
        str(ROOT / "runtime" / "recordgate" / "start_app_gate.template"),
        "runtime/recordgate",
    )
]

a = Analysis(
    [str(WINBUILD / "patch_firmware.py")],
    pathex=[str(ROOT / "pak")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["pak_repack", "reolink_crc"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="patch_firmware",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
