import sys
from cx_Freeze import setup, Executable

# Dependencies are automatically detected, but it might need fine tuning.
build_exe_options = {
    "packages": [
        "os",
        "sys",
        "PyQt6.QtWidgets",
        "PyQt6.QtGui",
        "PyQt6.QtCore",
        "win32serviceutil",
        "win32service",
        "asyncio",
        "logging",
        "httpx",
        "configparser",
        "json",
    ],
    "excludes": [],
    "include_files": ["../icon.ico"],
}

base = None
if sys.platform == "win32":
    base = "Win32GUI"

setup(
    name="VideoGrouperTray",
    version="1.0",
    description="Video Grouper Tray Agent",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            "main.py", base=base, target_name="VideoGrouperTray.exe", icon="../icon.ico"
        )
    ],
)
