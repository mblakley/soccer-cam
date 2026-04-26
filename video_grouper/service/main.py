"""Windows Service wrapper for VideoGrouper."""

import os
import sys
import asyncio
import logging

import win32serviceutil
import win32service
import win32event
import servicemanager
import win32timezone  # noqa: F401 - required for pyinstaller


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
        from pathlib import Path
        from video_grouper.video_grouper_app import VideoGrouperApp
        from video_grouper.utils.config import load_config
        from video_grouper.utils.logger import setup_logging
        from video_grouper.utils.locking import FileLock

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
            exe_dir = Path(os.path.dirname(sys.executable))
            config_path = exe_dir / "config.ini"

        if not config_path.exists():
            logger.error(f"Config file not found at {config_path}")
            return

        logger.info(f"Loading config from {config_path}")
        try:
            with FileLock(config_path):
                config = load_config(config_path)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return

        # Set CWD to storage path so relative paths resolve correctly.
        # Windows services default to C:\WINDOWS\system32 which breaks
        # state.json lock files, video paths, and everything else.
        storage_dir = config_path.parent
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
