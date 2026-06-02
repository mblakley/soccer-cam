"""Windows Service wrapper for VideoGrouper."""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import servicemanager
import win32event
import win32service
import win32serviceutil
import win32timezone  # noqa: F401 - required for pyinstaller

if TYPE_CHECKING:
    from video_grouper.utils.config import Config


def _resolve_storage_cwd(config: "Config") -> Path:
    """Return the directory the service should chdir into.

    Must be the configured ``[STORAGE] path`` rather than the
    config-file's parent. The two are usually different: config.ini
    lives under ``%ProgramData%\\VideoGrouper`` (for write-access
    reasons), but the user's storage tree is typically on a separate
    drive. If CWD points at the config dir, then DirectoryState's
    bare-basename fallback and any ``.lock`` file written via a
    relative path land under ``%ProgramData%`` instead of the storage
    drive, producing ``FileNotFoundError`` on every lock acquire.
    """
    return Path(config.storage.path)


class VideoGrouperService(win32serviceutil.ServiceFramework):
    _svc_name_ = "VideoGrouperService"
    _svc_display_name_ = "VideoGrouper Service"
    _svc_description_ = "Automated soccer video processing pipeline"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.running = False
        self.loop = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        # Report RUNNING immediately so SCM doesn't time out during the
        # slow Python import + config load phase. The asyncio loop is
        # spun up in self.main() afterward. Without this, `sc start`
        # fails with 1053 on cold-start even though the service is fine.
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        self.running = True
        self.main()

    def main(self):
        from video_grouper.utils.config import load_config
        from video_grouper.utils.locking import FileLock
        from video_grouper.utils.logger import setup_logging
        from video_grouper.video_grouper_app import VideoGrouperApp

        setup_logging(level="INFO", app_name="video_grouper")
        logger = logging.getLogger(__name__)

        # Find config: env var > registry > exe directory
        config_path = None
        env_config = os.environ.get("VIDEOGROUPER_CONFIG")
        if env_config:
            config_path = Path(env_config)
            logger.info(f"Using config from VIDEOGROUPER_CONFIG: {config_path}")

        if not config_path or not config_path.exists():
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, r"Software\VideoGrouper"
                )
                storage_path = winreg.QueryValueEx(key, "StoragePath")[0]
                winreg.CloseKey(key)
                config_path = Path(storage_path) / "config.ini"
            except Exception:
                pass

        if not config_path or not config_path.exists():
            # Fallback to %PROGRAMDATA%\VideoGrouper. Writing config + state
            # under Program Files (the install dir) requires admin and litters
            # protected paths; ProgramData is the canonical Windows home for
            # per-machine app state.
            program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
            config_path = program_data / "VideoGrouper" / "config.ini"

        if not config_path.exists():
            # Phase 2 done-criterion: a fresh shared_data with no
            # config.ini still boots the service; the dashboard will
            # bounce the user to /setup/welcome.
            from video_grouper.utils.config import create_default_config

            logger.info(f"No config at {config_path}; writing onboarding stub.")
            config_path.parent.mkdir(parents=True, exist_ok=True)
            create_default_config(config_path, str(config_path.parent))

        logger.info(f"Loading config from {config_path}")
        try:
            with FileLock(config_path):
                config = load_config(config_path)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return

        # Set CWD to configured storage path so relative paths resolve
        # correctly. Windows services default to C:\WINDOWS\system32 which
        # breaks state.json lock files, video paths, and everything else.
        # NOTE: must use config.storage.path, not config_path.parent --
        # the config typically lives under %ProgramData%\VideoGrouper but
        # the storage tree is usually on a separate drive.
        storage_dir = _resolve_storage_cwd(config)
        storage_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(storage_dir)
        logger.info(f"Set working directory to {storage_dir}")

        app = VideoGrouperApp(config, config_path=config_path)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.loop.run_until_complete(app.run())
        except Exception as e:
            logger.error(f"Service error: {e}")
        finally:
            try:
                self.loop.run_until_complete(app.shutdown())
            except Exception:
                pass
            self.loop.close()


def main():
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(VideoGrouperService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(VideoGrouperService)


if __name__ == "__main__":
    main()
