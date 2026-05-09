# -*- mode: python ; coding: utf-8 -*-
"""Multi-target PyInstaller spec — service + tray share one _internal/.

Both the Windows service and the system tray import the same large
chunk of soccer-cam (task_processors, integrations, web routers).
Building them as separate `pyinstaller --onedir` outputs ships two
copies of the entire dependency tree (~1.8 GB raw, ~370 MB
compressed).  This spec runs one Analysis per entry point, MERGEs
the shared bits, and emits a single COLLECT — so dist/VideoGrouper/
contains both .exes plus one shared _internal/.

Excludes drop the training-only ML stack (torch / ultralytics / scipy
/ matplotlib / sympy / networkx). Inference modules need numpy +
onnxruntime + cv2 only — those stay bundled.

Build:
    uv run pyinstaller --noconfirm \
        --distpath video_grouper/dist \
        --workpath video_grouper/build \
        video_grouper/installer/VideoGrouper.spec
"""

import os

# PyInstaller resolves relative paths in a .spec file relative to the
# spec's own directory. The spec lives at video_grouper/installer/, so
# the entry points and icon live one directory up.
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
PROJECT_ROOT = os.path.abspath(os.path.join(SPEC_DIR, "..", ".."))
SERVICE_MAIN = os.path.join(PROJECT_ROOT, "video_grouper", "service", "main.py")
TRAY_MAIN = os.path.join(PROJECT_ROOT, "video_grouper", "tray", "main.py")
ICON_PATH = os.path.join(PROJECT_ROOT, "video_grouper", "icon.ico")

EXCLUDES = [
    # Training stack (torch alone is 3.85 GB)
    "torch",
    "torchvision",
    "ultralytics",
    "ultralytics_thop",
    # torch's transitive bloat
    "scipy",
    "matplotlib",
    "sympy",
    "networkx",
]

a_service = Analysis(
    [SERVICE_MAIN],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

a_tray = Analysis(
    [TRAY_MAIN],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Deduplicate: tag every shared dep as belonging to the service so the
# tray Analysis only carries what's exclusive to it.
MERGE(
    (a_service, "VideoGrouperService", "VideoGrouperService"),
    (a_tray, "VideoGrouperTray", "VideoGrouperTray"),
)

pyz_service = PYZ(a_service.pure)
pyz_tray = PYZ(a_tray.pure)

exe_service = EXE(
    pyz_service,
    a_service.scripts,
    [],
    exclude_binaries=True,
    name="VideoGrouperService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=ICON_PATH,
)

exe_tray = EXE(
    pyz_tray,
    a_tray.scripts,
    [],
    exclude_binaries=True,
    name="VideoGrouperTray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=ICON_PATH,
)

coll = COLLECT(
    exe_service,
    a_service.binaries,
    a_service.datas,
    exe_tray,
    a_tray.binaries,
    a_tray.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="VideoGrouper",
)
