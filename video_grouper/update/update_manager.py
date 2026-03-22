import os
import sys
import subprocess
import json
import httpx
import logging
import tempfile
import shutil
from typing import Tuple, Optional, TypedDict

from packaging.version import Version, InvalidVersion

logger = logging.getLogger(__name__)

REQUIRED_ASSETS = ("VideoGrouperService.exe", "VideoGrouperTray.exe")


class VersionInfo(TypedDict, total=False):
    """Represents version information from a GitHub Release."""

    version: str
    tag_name: str
    release_date: str
    release_notes: str
    assets: list[dict]


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
    def __init__(
        self,
        current_version: str,
        github_repo: str,
        service_name: str = "VideoGrouperService",
    ):
        self.current_version = current_version
        self.github_repo = github_repo
        self.service_name = service_name
        self.temp_dir = tempfile.mkdtemp()
        self.timeout = httpx.Timeout(
            300.0, connect=10.0
        )  # 300s read timeout, 10s connect

    async def check_for_updates(self) -> Tuple[bool, Optional[VersionInfo]]:
        """
        Check GitHub Releases for a newer version.
        Returns a tuple of (has_update, version_info).
        """
        api_url = f"https://api.github.com/repos/{self.github_repo}/releases/latest"
        headers = {"Accept": "application/vnd.github+json"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    response = await client.get(api_url, headers=headers)

                    if response.status_code == 404:
                        logger.info("No releases found for this repository")
                        return False, None

                    response.raise_for_status()
                    release = response.json()

                except httpx.HTTPStatusError as e:
                    logger.error(f"HTTP error checking for updates: {e}")
                    return False, None
                except httpx.RequestError as e:
                    logger.error(f"Network error checking for updates: {e}")
                    raise NetworkError(f"Failed to connect to GitHub API: {e}")
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from GitHub API: {e}")
                    return False, None

        except Exception as e:
            if not isinstance(e, UpdateError):
                logger.error(f"Unexpected error checking for updates: {e}")
                raise UpdateCheckError(f"Failed to check for updates: {e}")
            raise

        # Parse version from tag
        tag_name = release.get("tag_name", "")
        version_str = tag_name.lstrip("v")

        try:
            remote_version = Version(version_str)
            local_version = Version(self.current_version)
        except InvalidVersion as e:
            logger.error(f"Invalid version format: {e}")
            return False, None

        if remote_version <= local_version:
            logger.debug(
                f"No update needed: local={local_version}, remote={remote_version}"
            )
            return False, None

        # Check that required assets are present
        assets = release.get("assets", [])
        asset_names = {a["name"] for a in assets}
        if not set(REQUIRED_ASSETS).issubset(asset_names):
            logger.info(
                f"Release {tag_name} exists but required assets not ready yet. "
                f"Found: {asset_names}, need: {set(REQUIRED_ASSETS)}"
            )
            return False, None

        version_info: VersionInfo = {
            "version": version_str,
            "tag_name": tag_name,
            "release_date": release.get("published_at", ""),
            "release_notes": release.get("body", ""),
            "assets": assets,
        }

        logger.info(f"Update available: {local_version} -> {remote_version}")
        return True, version_info

    async def download_file(self, url: str, file_path: str) -> bool:
        """Download a file with progress tracking and error handling."""
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                try:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        total_size = int(response.headers.get("content-length", 0))

                        with open(file_path, "wb") as f:
                            downloaded = 0
                            last_logged = -1
                            async for chunk in response.aiter_bytes():
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if total_size > 0:
                                        pct = int((downloaded / total_size) * 100)
                                        tens = pct // 10
                                        if tens > last_logged:
                                            last_logged = tens
                                            logger.info(f"Download progress: {pct}%")

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

    async def download_update(self, version_info: VersionInfo) -> bool:
        """Download the new version executables from GitHub Release assets."""
        try:
            assets_by_name = {a["name"]: a for a in version_info["assets"]}

            for exe_name in REQUIRED_ASSETS:
                asset = assets_by_name.get(exe_name)
                if not asset:
                    logger.error(f"Asset {exe_name} not found in release")
                    return False

                url = asset["browser_download_url"]
                dest = os.path.join(self.temp_dir, exe_name)

                logger.info(f"Downloading {exe_name}...")
                if not await self.download_file(url, dest):
                    return False

            # Verify downloaded files exist and are non-empty
            for exe_name in REQUIRED_ASSETS:
                path = os.path.join(self.temp_dir, exe_name)
                if not os.path.exists(path) or os.path.getsize(path) == 0:
                    logger.error(f"Downloaded {exe_name} is missing or empty")
                    return False

            return True

        except Exception as e:
            logger.error(f"Error downloading update: {e}")
            return False

    def install_update(self) -> bool:
        """Install the downloaded update with error handling and rollback."""
        import win32serviceutil

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

            # Kill the tray process so its exe can be overwritten
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "VideoGrouperTray.exe"],
                    capture_output=True,
                )
            except Exception as e:
                logger.warning(f"Could not kill tray process: {e}")

            # Prepare file paths
            service_src = os.path.join(self.temp_dir, "VideoGrouperService.exe")
            service_dst = os.path.join(install_dir, "VideoGrouperService.exe")

            tray_src = os.path.join(self.temp_dir, "VideoGrouperTray.exe")
            tray_dst = os.path.join(install_dir, "VideoGrouperTray.exe")

            # Backup existing files
            for src, dst in [
                (service_dst, f"{service_dst}.bak"),
                (tray_dst, f"{tray_dst}.bak"),
            ]:
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


async def check_and_update(
    current_version: str,
    github_repo: str,
    service_name: str = "VideoGrouperService",
) -> bool:
    """
    Convenience function to check for and install updates.
    Returns True if an update was successfully installed, False otherwise.
    """
    update_manager = UpdateManager(current_version, github_repo, service_name)
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
