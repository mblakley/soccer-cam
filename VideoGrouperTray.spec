# -*- mode: python ; coding: utf-8 -*-

# TRAY/SERVICE SPLIT (excludes): the tray bundle drives ONLY the autocam (GUI)
# step on the interactive desktop — it never runs detect/track/render. So it
# additionally excludes the whole inference stack (onnxruntime, cv2, av) on top
# of the heavy ML libs (torch/torchvision/ultralytics/scipy/...), keeping the
# tray light. With those modules absent, pipeline.register_steps' per-step
# try/except simply omits detect/track/render, leaving autocam registered.
# The SERVICE spec (VideoGrouperService.spec) is the mirror image: it RETAINS
# onnxruntime/cv2/av because it runs detect/render in Session 0, while still
# excluding torch/scipy.

a = Analysis(
    ['video_grouper\\tray\\main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['onnxruntime', 'cv2', 'av', 'torch', 'torchvision', 'ultralytics', 'ultralytics_thop', 'scipy', 'matplotlib', 'sympy', 'networkx'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoGrouperTray',
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
    name='VideoGrouperTray',
)
