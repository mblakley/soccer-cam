# -*- mode: python ; coding: utf-8 -*-

# TRAY/SERVICE SPLIT (excludes): the service runs the headless pipeline steps
# in Session 0 — detect (onnxruntime + cv2) and stitch_correct/render (av) — so
# it MUST RETAIN onnxruntime, cv2, and av. It still excludes the heavy training
# libs (torch/torchvision/ultralytics/scipy/...) that inference doesn't need.
# The TRAY spec (VideoGrouperTray.spec) is the mirror image: it ADDS
# onnxruntime/cv2/av to its excludes because it only drives the autocam (GUI)
# step and must stay light.

a = Analysis(
    ['video_grouper\\service\\main.py'],
    pathex=[],
    binaries=[],
    datas=[],
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
