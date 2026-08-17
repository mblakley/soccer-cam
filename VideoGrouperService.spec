# -*- mode: python ; coding: utf-8 -*-

# TRAY/SERVICE SPLIT (excludes): the service runs the headless pipeline steps
# in Session 0 — detect (onnxruntime + cv2) and stitch_correct/render (av) — so
# it MUST RETAIN onnxruntime, cv2, and av. It still excludes the heavy training
# libs (torch/torchvision/ultralytics/scipy/...) that inference doesn't need.
# The TRAY spec (VideoGrouperTray.spec) is the mirror image: it ADDS
# onnxruntime/cv2/av to its excludes because it only drives the autocam (GUI)
# step and must stay light.

# The seam-calibration tool at /stitch reaches the firmware-side toolkit
# (seam_metric, seam_vertical, stitch_apply, lut2d, camsh) by path rather than
# by import --
# `reolink-firmware-patching/` is excluded from the wheel and from mypy because
# it is firmware territory, not application code. Carrying the two directories
# in as data keeps that separation while letting the installed service run the
# calibration UI; `stitch_calibration._vpe_dir()` looks for them under
# sys._MEIPASS, and stitch_apply's own `parents[1]/"runtime"` lookup lands
# correctly given this layout. Service spec only: the tray excludes cv2/av, so
# these modules could not import there anyway.
a = Analysis(
    ['video_grouper\\service\\main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('reolink-firmware-patching\\vpe', 'vpe'),
        ('reolink-firmware-patching\\runtime', 'runtime'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'ultralytics', 'ultralytics_thop', 'scipy', 'matplotlib', 'sympy', 'networkx'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoGrouperService',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['video_grouper\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoGrouperService',
)
