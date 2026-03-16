# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['video_grouper\\tray\\tray_entry.py'],
    pathex=['.'],
    binaries=[],
    datas=[('video_grouper/icon.ico', 'video_grouper')],
    hiddenimports=[
        'video_grouper.task_processors.tasks.video.combine_task',
        'video_grouper.task_processors.tasks.video.trim_task',
        'video_grouper.task_processors.tasks.ntfy.game_start_task',
        'video_grouper.task_processors.tasks.ntfy.game_end_task',
        'video_grouper.task_processors.tasks.ntfy.team_info_task',
        'video_grouper.task_processors.tasks.upload.youtube_upload_task',
        'video_grouper.task_processors.tasks.clip.clip_request_task',
        'video_grouper.task_processors.register_tasks',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VideoGrouperTray',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['video_grouper\\icon.ico'],
)
