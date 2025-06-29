import win32serviceutil
import win32service
import win32event
import servicemanager
import socket
import sys
import os
import logging
import asyncio
from pathlib import Path
from video_grouper.video_grouper_app import VideoGrouperApp
from video_grouper.update.update_manager import check_and_update
from video_grouper.version import get_version, get_full_version
from video_grouper.utils.paths import get_shared_data_path
from video_grouper.utils.locking import FileLock
from video_grouper.utils.config import load_config, Config
from typing import Optional

# Configure logging
logging.basicConfig(
    filename='C:\\ProgramData\\VideoGrouper\\service.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

class VideoGrouperService(win32serviceutil.ServiceFramework):
    _svc_name_ = "VideoGrouperService"
    _svc_display_name_ = "Video Grouper Service"
    _svc_description_ = "Service for managing video grouping operations"
    
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.running = False
        self.version = get_version()
        self.full_version = get_full_version()
        
        # Load configuration
        self.config: Optional[Config] = None
        self.config_path = get_shared_data_path() / 'config.ini'
        try:
            with FileLock(self.config_path):
                if self.config_path.exists():
                    self.config = load_config(self.config_path)
        except TimeoutError as e:
            logger.error(f"Could not acquire lock to read config file for service: {e}")
        except Exception as e:
            logger.error(f"Error loading configuration for service: {e}")
            
        # Get update URL from config
        self.update_url = self.config.app.update_url if self.config else 'https://updates.videogrouper.com'
        
    def SvcStop(self):
        """Stop the service."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        self.running = False
        
    def SvcDoRun(self):
        """Run the service."""
        self.running = True
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, f'Version {self.full_version}')
        )
        
        try:
            # Run the main service and update checker concurrently
            asyncio.run(self.run_service())
        except Exception as e:
            logger.error(f"Service error: {e}")
            servicemanager.LogErrorMsg(f"Service error: {e}")
            
    async def run_service(self):
        """Run the main service and update checker."""
        try:
            # Start both tasks
            await asyncio.gather(
                self.run_main_service(),
                self.check_updates()
            )
        except Exception as e:
            logger.error(f"Error in service tasks: {e}")
            raise
            
    async def run_main_service(self):
        """Run the main service functionality."""
        try:
            app = VideoGrouperApp(self.config)
            await app.run()
        except Exception as e:
            logger.error(f"Error in main service: {e}")
            raise
            
    async def check_updates(self):
        """Check for updates periodically."""
        while self.running:
            try:
                # Check for updates every hour
                await check_and_update(self.version, self.update_url)
                await asyncio.sleep(3600)  # Sleep for 1 hour
            except Exception as e:
                logger.error(f"Error checking for updates: {e}")
                await asyncio.sleep(300)  # Sleep for 5 minutes on error
                
def main():
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(VideoGrouperService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(VideoGrouperService)
        
if __name__ == '__main__':
    main() 