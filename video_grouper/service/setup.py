from cx_Freeze import setup, Executable

# Dependencies are automatically detected, but it might need fine tuning.
build_exe_options = {
    "packages": [
        "os",
        "sys",
        "win32serviceutil",
        "win32service",
        "win32event",
        "servicemanager",
        "socket",
        "asyncio",
        "logging",
        "httpx",
        "aiofiles",
        "configparser",
        "json",
        "datetime",
        "signal",
    ],
    "excludes": [],
    "include_files": [],
}

base = None
# Service executables are command-line apps, not GUI apps
# if sys.platform == "win32":
#     base = "Win32GUI"

setup(
    name="VideoGrouperService",
    version="1.0",
    description="Video Grouper Windows Service",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            "main.py", base=base, target_name="VideoGrouperService.exe", icon=None
        )
    ],
)
