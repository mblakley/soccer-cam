import os
import sys
import json
import httpx
import logging
import win32serviceutil
import tempfile
import shutil
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)

class UpdateError(Exception):
    """Base class for update-related errors."""
    pass

class NetworkError(UpdateError):
    """Raised when there are network-related issues."""
    pass

class UpdateCheckError(UpdateError):
    """Raised when there are issues checking for updates."""
    pass

class UpdateDownloadError(UpdateError):
    """Raised when there are issues downloading updates."""
    pass

class UpdateInstallError(UpdateError):
    """Raised when there are issues installing updates."""
    pass

class UpdateManager:
    def __init__(self, current_version: str, update_url: str, service_name: str = "VideoGrouperService"):
        self.current_version = current_version
        self.update_url = update_url
        self.service_name = service_name
        self.temp_dir = tempfile.mkdtemp()
        self.timeout = httpx.Timeout(10.0, connect=5.0)  # 10 second timeout, 5 second connect timeout
        
    async def check_for_updates(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check if a new version is available.
        Returns a tuple of (has_update, version_info).
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    response = await client.get(f"{self.update_url}/version")
                    response.raise_for_status()
                    version_info = response.json()
                    
                    # Validate version info
                    if not isinstance(version_info, dict) or "version" not in version_info:
                        logger.error("Invalid version info format received")
                        return False, None
                        
                    return version_info["version"] > self.current_version, version_info
                    
                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP error checking for updates: {e}")
                    return False, None
                except httpx.RequestError as e:
                    logger.error(f"Network error checking for updates: {e}")
                    raise NetworkError(f"Failed to connect to update server: {e}")
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON response from update server: {e}")
                    return False, None
                    
        except Exception as e:
            if not isinstance(e, UpdateError):
                logger.error(f"Unexpected error checking for updates: {e}")
                raise UpdateCheckError(f"Failed to check for updates: {e}")
            raise
            
    async def download_file(self, url: str, file_path: str) -> bool:
        """Download a file with progress tracking and error handling."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        total_size = int(response.headers.get("content-length", 0))
                        
                        with open(file_path, "wb") as f:
                            downloaded = 0
                            async for chunk in response.aiter_bytes():
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    # Log progress every 10%
                                    if total_size > 0:
                                        progress = (downloaded / total_size) * 100
                                        if int(progress) % 10 == 0:
                                            logger.info(f"Download progress: {progress:.1f}%")
                                            
                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP error downloading file: {e}")
                    return False
                except httpx.RequestError as e:
                    logger.error(f"Network error downloading file: {e}")
                    raise NetworkError(f"Failed to download file: {e}")
                    
            return True
            
        except Exception as e:
            if not isinstance(e, UpdateError):
                logger.error(f"Unexpected error downloading file: {e}")
                raise UpdateDownloadError(f"Failed to download file: {e}")
            raise
            
    async def download_update(self, version_info: Dict[str, Any]) -> bool:
        """Download the new version with error handling."""
        try:
            # Download service executable
            service_url = f"{self.update_url}/download/service/{version_info['version']}"
            service_path = os.path.join(self.temp_dir, "VideoGrouperService.exe")
            
            if not await self.download_file(service_url, service_path):
                return False
            
            # Download tray agent executable
            tray_url = f"{self.update_url}/download/tray/{version_info['version']}"
            tray_path = os.path.join(self.temp_dir, "tray_agent.exe")
            
            if not await self.download_file(tray_url, tray_path):
                return False
            
            # Verify downloaded files
            if not os.path.exists(service_path) or not os.path.exists(tray_path):
                logger.error("Downloaded files not found")
                return False
                
            # Verify file sizes
            if os.path.getsize(service_path) == 0 or os.path.getsize(tray_path) == 0:
                logger.error("Downloaded files are empty")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error downloading update: {e}")
            return False
            
    def install_update(self) -> bool:
        """Install the downloaded update with error handling and rollback."""
        backup_files = []
        try:
            # Get installation directory
            install_dir = os.path.dirname(sys.executable)
            
            # Stop the service
            try:
                win32serviceutil.StopService(self.service_name)
            except Exception as e:
                logger.error(f"Error stopping service: {e}")
                raise UpdateInstallError(f"Failed to stop service: {e}")
            
            # Prepare file paths
            service_src = os.path.join(self.temp_dir, "VideoGrouperService.exe")
            service_dst = os.path.join(install_dir, "VideoGrouperService.exe")
            
            tray_src = os.path.join(self.temp_dir, "tray_agent.exe")
            tray_dst = os.path.join(install_dir, "tray_agent.exe")
            
            # Backup existing files
            for src, dst in [(service_dst, f"{service_dst}.bak"), (tray_dst, f"{tray_dst}.bak")]:
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    backup_files.append((src, dst))
            
            # Copy new files
            shutil.copy2(service_src, service_dst)
            shutil.copy2(tray_src, tray_dst)
            
            # Start the service
            try:
                win32serviceutil.StartService(self.service_name)
            except Exception as e:
                logger.error(f"Error starting service: {e}")
                self._restore_backups(backup_files)
                raise UpdateInstallError(f"Failed to start service: {e}")
            
            # Clean up
            self._cleanup_backups(backup_files)
            return True
            
        except Exception as e:
            logger.error(f"Error installing update: {e}")
            self._restore_backups(backup_files)
            return False
            
    def _restore_backups(self, backup_files: list) -> None:
        """Restore backup files."""
        for src, backup in backup_files:
            try:
                if os.path.exists(backup):
                    shutil.copy2(backup, src)
            except Exception as e:
                logger.error(f"Error restoring backup {backup}: {e}")
                
    def _cleanup_backups(self, backup_files: list) -> None:
        """Clean up backup files."""
        for _, backup in backup_files:
            try:
                if os.path.exists(backup):
                    os.remove(backup)
            except Exception as e:
                logger.error(f"Error cleaning up backup {backup}: {e}")
                
    def cleanup(self) -> None:
        """Clean up temporary files."""
        try:
            shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up: {e}")

async def check_and_update(current_version: str, update_url: str, service_name: str = "VideoGrouperService") -> bool:
    """
    Convenience function to check for and install updates.
    Returns True if an update was successfully installed, False otherwise.
    """
    update_manager = UpdateManager(current_version, update_url, service_name)
    try:
        try:
            has_update, version_info = await update_manager.check_for_updates()
        except NetworkError as e:
            logger.warning(f"Network error checking for updates: {e}")
            return False
        except UpdateCheckError as e:
            logger.error(f"Error checking for updates: {e}")
            return False
            
        if has_update:
            logger.info(f"New version {version_info['version']} available")
            try:
                if await update_manager.download_update(version_info):
                    if update_manager.install_update():
                        logger.info("Update installed successfully")
                        return True
            except NetworkError as e:
                logger.warning(f"Network error during update: {e}")
            except (UpdateDownloadError, UpdateInstallError) as e:
                logger.error(f"Error during update: {e}")
        return False
    finally:
        update_manager.cleanup() 