"""Google Drive file uploader using YouTube OAuth client credentials."""

import logging
import mimetypes
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class GoogleDriveUploader:
    """Uploads files to Google Drive, reusing the YouTube OAuth client credentials."""

    def __init__(self, storage_path: str) -> None:
        self.storage_path = storage_path
        self.client_secret_path = os.path.join(
            storage_path, "youtube", "client_secret.json"
        )
        self.token_dir = os.path.join(storage_path, "google_drive")
        self.token_path = os.path.join(self.token_dir, "token.json")
        self.credentials: Credentials | None = None
        self.service = None

    def authenticate(self, interactive: bool = False) -> None:
        """Authenticate with Google Drive API.

        Args:
            interactive: If True, open a browser for OAuth consent when needed.
                If False, raise an error when no valid token is available.

        Raises:
            RuntimeError: If no valid token exists and interactive mode is disabled.
            FileNotFoundError: If the client_secret.json file is missing.
        """
        if not os.path.exists(self.client_secret_path):
            raise FileNotFoundError(
                f"OAuth client secret not found at {self.client_secret_path}. "
                "Ensure YouTube OAuth credentials are set up first."
            )

        creds: Credentials | None = None

        if os.path.exists(self.token_path):
            logger.info("Loading existing Google Drive token from %s", self.token_path)
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if creds and creds.valid:
            logger.info("Existing Google Drive token is valid.")
            self.credentials = creds
            self.service = build("drive", "v3", credentials=self.credentials)
            return

        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Google Drive token.")
            try:
                creds.refresh(Request())
                self._save_token(creds)
                self.credentials = creds
                self.service = build("drive", "v3", credentials=self.credentials)
                return
            except Exception:
                logger.warning("Token refresh failed. Re-authentication required.")
                creds = None

        if not interactive:
            raise RuntimeError(
                "No valid Google Drive token available and interactive mode is disabled. "
                "Run with interactive=True to authenticate via browser."
            )

        logger.info("Starting interactive OAuth flow for Google Drive.")
        flow = InstalledAppFlow.from_client_secrets_file(
            self.client_secret_path, SCOPES
        )
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        self.credentials = creds
        self.service = build("drive", "v3", credentials=self.credentials)
        logger.info("Google Drive authentication successful.")

    def _save_token(self, creds: Credentials) -> None:
        """Save OAuth token to disk."""
        Path(self.token_dir).mkdir(parents=True, exist_ok=True)
        with open(self.token_path, "w") as f:
            f.write(creds.to_json())
        logger.info("Google Drive token saved to %s", self.token_path)

    def upload_file(
        self, file_path: str, folder_id: str, filename: str | None = None
    ) -> str:
        """Upload a file to a Google Drive folder.

        Args:
            file_path: Path to the local file to upload.
            folder_id: Google Drive folder ID to upload into.
            filename: Optional name for the file in Drive. Defaults to the local filename.

        Returns:
            The Google Drive file ID of the uploaded file.

        Raises:
            RuntimeError: If not authenticated.
            FileNotFoundError: If the local file does not exist.
        """
        if self.service is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        if filename is None:
            filename = os.path.basename(file_path)

        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        file_metadata: dict = {
            "name": filename,
            "parents": [folder_id],
        }

        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

        logger.info(
            "Uploading '%s' to Google Drive folder %s (MIME: %s)",
            filename,
            folder_id,
            mime_type,
        )

        file = (
            self.service.files()
            .create(body=file_metadata, media_body=media, fields="id")
            .execute()
        )

        file_id: str = file.get("id")
        logger.info("Upload complete. File ID: %s", file_id)
        return file_id

    def create_shareable_link(self, file_id: str) -> str:
        """Set a file to 'anyone with the link can view' and return the share URL.

        Args:
            file_id: Google Drive file ID.

        Returns:
            The shareable URL for the file.

        Raises:
            RuntimeError: If not authenticated.
        """
        if self.service is None:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        permission: dict = {
            "type": "anyone",
            "role": "reader",
        }

        self.service.permissions().create(
            fileId=file_id,
            body=permission,
            fields="id",
        ).execute()

        share_url = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        logger.info("Shareable link created: %s", share_url)
        return share_url

    def upload_and_share(
        self, file_path: str, folder_id: str, filename: str | None = None
    ) -> str:
        """Upload a file and create a shareable link.

        Args:
            file_path: Path to the local file to upload.
            folder_id: Google Drive folder ID to upload into.
            filename: Optional name for the file in Drive. Defaults to the local filename.

        Returns:
            The shareable URL for the uploaded file.
        """
        file_id = self.upload_file(file_path, folder_id, filename)
        return self.create_shareable_link(file_id)
