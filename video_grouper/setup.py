import sys
from cx_Freeze import setup, Executable

# Dependencies are automatically detected, but it might need fine tuning.
build_exe_options = {
    "packages": ["os", "sys", "win32serviceutil", "win32service", "win32event", 
                "servicemanager", "socket", "asyncio", "logging", "httpx", 
                "aiofiles", "configparser", "json", "datetime", "signal"],
    "excludes": [],
    "include_files": [
        "config.ini",
        "match_info.ini.dist",
        "requirements.txt"
    ]
}

base = None
if sys.platform == "win32":
    base = "Win32GUI"

setup(
    name="VideoGrouperService",
    version="1.0",
    description="Video Grouper Windows Service",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            "service_wrapper.py",
            base=base,
            target_name="VideoGrouperService.exe",
            icon=None  # Add icon path if you have one
        )
    ]
) 