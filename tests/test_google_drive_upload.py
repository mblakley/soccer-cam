"""Tests for GoogleDriveUploader OAuth client-type validation.

The Drive uploader reuses the YouTube OAuth client, which must be a Google
"Desktop app" client for the interactive loopback consent flow to work. A
"Web" client fails deep inside the flow with redirect_uri_mismatch; the
uploader surfaces that up front with an actionable error.

Note: conftest's autouse ``mock_file_system`` patches ``os.path.exists`` to
True for everything, so each test drives it via a side_effect to control which
of {client_secret.json, token.json} "exists". The real client_secret.json is
still written to disk (pathlib), so the uploader's real open()/JSON read works.
"""

import json

import pytest

from video_grouper.utils.google_drive_upload import GoogleDriveUploader


def _write_client_secret(storage, kind: str) -> None:
    d = storage / "youtube"
    d.mkdir(parents=True, exist_ok=True)
    (d / "client_secret.json").write_text(
        json.dumps({kind: {"client_id": "x", "client_secret": "y"}})
    )


def test_web_client_raises_helpful_error(tmp_path, mock_file_system):
    """A 'web' OAuth client → clear RuntimeError before the browser flow opens."""
    _write_client_secret(tmp_path, "web")
    # client_secret.json exists; token.json does not → reach the interactive flow
    mock_file_system["exists"].side_effect = lambda p: "client_secret" in str(p)
    uploader = GoogleDriveUploader(str(tmp_path))
    with pytest.raises(RuntimeError, match="Desktop app"):
        uploader.authenticate(interactive=True)


def test_missing_client_secret_raises_filenotfound(tmp_path, mock_file_system):
    mock_file_system["exists"].return_value = False
    uploader = GoogleDriveUploader(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        uploader.authenticate(interactive=True)


def test_non_interactive_without_token_raises_runtimeerror(tmp_path, mock_file_system):
    """An 'installed' client but no token + interactive=False → the existing
    'run with interactive=True' error (the Desktop check is only on the
    interactive path, so an installed client does not trip it)."""
    _write_client_secret(tmp_path, "installed")
    mock_file_system["exists"].side_effect = lambda p: "client_secret" in str(p)
    uploader = GoogleDriveUploader(str(tmp_path))
    with pytest.raises(RuntimeError, match="interactive"):
        uploader.authenticate(interactive=False)
