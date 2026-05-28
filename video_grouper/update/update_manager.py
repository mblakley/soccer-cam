import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import TypedDict

import httpx
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# The auto-upgrade path downloads the full NSIS installer and spawns
# it with /S; the installer in turn replaces service.exe + tray.exe
# + the shared _internal/ tree + writes the registry, all
# atomically. The legacy two-exe asset list was a Phase 1 transient.
# CI (`.github/workflows/build-windows-service.yml`) publishes only
# this single artifact to GitHub Releases.
REQUIRED_ASSETS = ("VideoGrouperSetup.exe",)
INSTALLER_ASSET = "VideoGrouperSetup.exe"

UPDATE_API_URL_ENV = "SOCCER_CAM_UPDATE_API_URL"


def resolve_api_url(github_repo: str, override: str | None = None) -> tuple[str, str]:
    """Pick the GitHub Releases endpoint. Returns (url, source).

    Precedence (highest first):

    1. ``SOCCER_CAM_UPDATE_API_URL`` env var — flipped for E2E testing
       (the test server at ``scripts/serve_test_release.py`` listens
       on 127.0.0.1 and serves a GitHub-shaped JSON response).
    2. ``override`` argument — pass ``config.app.update_api_url`` from
       the caller. Lets a sysadmin point a fleet at an internal mirror.
    3. The standard ``https://api.github.com/repos/{repo}/releases/latest``.

    ``source`` is one of ``"env"``, ``"config"``, ``"default"`` — for
    logging only.
    """
    env_url = os.environ.get(UPDATE_API_URL_ENV)
    if env_url:
        return env_url, "env"
    if override:
        return override, "config"
    return f"https://api.github.com/repos/{github_repo}/releases/latest", "default"


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
        api_url_override: str | None = None,
    ):
        self.current_version = current_version
        self.github_repo = github_repo
        self.service_name = service_name
        self.api_url_override = api_url_override
        self.temp_dir = tempfile.mkdtemp()
        self.timeout = httpx.Timeout(
            300.0, connect=10.0
        )  # 300s read timeout, 10s connect

    async def check_for_updates(self) -> tuple[bool, VersionInfo | None]:
        """
        Check the configured Releases endpoint for a newer version.
        Returns a tuple of (has_update, version_info).
        """
        api_url, source = resolve_api_url(self.github_repo, self.api_url_override)
        logger.info("Update source: %s (%s)", api_url, source)
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
                    raise NetworkError(f"Failed to connect to GitHub API: {e}") from e
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from GitHub API: {e}")
                    return False, None

        except Exception as e:
            if not isinstance(e, UpdateError):
                logger.error(f"Unexpected error checking for updates: {e}")
                raise UpdateCheckError(f"Failed to check for updates: {e}") from e
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
                    raise NetworkError(f"Failed to download file: {e}") from e

            return True

        except Exception as e:
            if not isinstance(e, UpdateError):
                logger.error(f"Unexpected error downloading file: {e}")
                raise UpdateDownloadError(f"Failed to download file: {e}") from e
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

    def installer_path(self) -> str:
        """Disk path to the downloaded setup.exe."""
        return os.path.join(self.temp_dir, INSTALLER_ASSET)

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Return ``sha256:<hex>`` for a downloaded asset, matching the
        format GitHub Releases uses in the asset ``digest`` field."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"

    def verify_digest(self, file_path: str, expected: str | None) -> bool:
        """Compare a downloaded artifact against the expected
        ``sha256:...`` digest. Treats a missing ``expected`` as a
        pass with a loud warning -- not all GitHub releases populate
        the digest field yet, and refusing to upgrade in that case
        would brick the rollout for older releases. The local server
        in ``scripts/serve_test_release.py`` always sets it, so the
        E2E test still exercises the verify branch."""
        if not expected:
            logger.warning(
                "No expected digest for %s -- proceeding without verification. "
                "Newer GitHub releases include this; older ones may not.",
                os.path.basename(file_path),
            )
            return True
        actual = self.compute_sha256(file_path)
        if actual.lower() != expected.lower():
            logger.error(
                "Digest mismatch for %s: expected=%s actual=%s",
                os.path.basename(file_path),
                expected,
                actual,
            )
            return False
        logger.info(
            "Digest OK for %s: %s",
            os.path.basename(file_path),
            actual,
        )
        return True

    def spawn_installer(self, installer_path: str | None = None) -> int:
        """Launch ``VideoGrouperSetup.exe /S`` detached from the
        service process and return its PID.

        The installer is the single source of deployment truth -- it
        stops the service, kills the tray, swaps service.exe +
        tray.exe + _internal/ in one ``File /r`` (atomic), writes
        the registry, restarts the service, and (Phase 4) launches
        the tray via the scheduled task. Re-using it guarantees an
        auto-upgrade install is bit-identical to a fresh install.

        The detached flags matter: NSIS calls ``sc stop
        VideoGrouperService`` early in its install, which would
        SIGTERM our process if we were the parent. By the time NSIS
        runs the service has already exited cleanly (see
        ``VideoGrouperApp._shutdown_event``), and the helper survives
        as an orphaned process group.
        """
        path = installer_path or self.installer_path()
        if not os.path.exists(path):
            raise UpdateInstallError(f"Installer not found at {path}")

        if sys.platform == "win32":
            DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            CREATE_NEW_PROCESS_GROUP = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            )
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            # Local-test convenience on non-Windows: just spawn it.
            # Real installer only runs on Windows; tests stub Popen.
            creationflags = 0

        logger.info("Spawning installer: %s /S", path)
        proc = subprocess.Popen(
            [path, "/S"],
            creationflags=creationflags,
            close_fds=True,
        )
        logger.info("Installer spawned with pid=%d", proc.pid)
        return proc.pid

    def cleanup(self) -> None:
        """Clean up temporary files."""
        try:
            shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up: {e}")
