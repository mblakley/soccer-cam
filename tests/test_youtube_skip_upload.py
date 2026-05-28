"""Unit tests for [YOUTUBE] skip_upload config flag and YouTubeUploader's
short-circuit path.

skip_upload is the smoke-test escape hatch that bypasses the Google API
without needing OAuth credentials on disk. Production must never run with
skip_upload=true (downstream consumers would get reels referencing
non-existent YouTube videos).
"""

from __future__ import annotations

import configparser
import os
import tempfile
from pathlib import Path

from video_grouper.utils.config import YouTubeConfig, load_config
from video_grouper.utils.youtube_upload import YouTubeUploader


def _base_parser() -> configparser.ConfigParser:
    p = configparser.ConfigParser()
    for section, values in {
        "CAMERA": {
            "type": "simulator",
            "device_ip": "0.0.0.0",
            "username": "a",
            "password": "b",
        },
        "STORAGE": {"path": "/tmp/test"},
        "RECORDING": {},
        "PROCESSING": {},
        "LOGGING": {},
        "APP": {},
        "TEAMSNAP": {},
        "PLAYMETRICS": {},
        "NTFY": {},
        "YOUTUBE": {},
        "AUTOCAM": {},
        "CLOUD_SYNC": {},
    }.items():
        p.add_section(section)
        for k, v in values.items():
            p.set(section, k, v)
    return p


def _write(parser: configparser.ConfigParser) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False)
    parser.write(tmp)
    tmp.close()
    return Path(tmp.name)


class TestSkipUploadConfig:
    def test_defaults_off(self):
        cfg = YouTubeConfig()
        assert cfg.skip_upload is False

    def test_parses_from_config(self):
        parser = _base_parser()
        parser.set("YOUTUBE", "skip_upload", "true")
        cfg = load_config(_write(parser))
        assert cfg.youtube.skip_upload is True

    def test_default_off_when_not_set(self):
        parser = _base_parser()
        cfg = load_config(_write(parser))
        assert cfg.youtube.skip_upload is False


class TestYouTubeUploaderSkipUpload:
    def test_skip_upload_returns_fake_video_id_for_existing_file(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fake_mp4:
            fake_mp4.write(b"not a real mp4 but the path exists")
            fake_mp4_path = fake_mp4.name

        try:
            uploader = YouTubeUploader(
                credentials_file="/does/not/exist.json",
                token_file="/does/not/exist.json",
                skip_upload=True,
            )
            video_id = uploader.upload_video(
                video_path=fake_mp4_path,
                title="Smoke title",
                description="Smoke description",
            )
            assert video_id is not None
            assert video_id.startswith("smoke-")
            assert len(video_id) == len("smoke-") + 11
        finally:
            os.unlink(fake_mp4_path)

    def test_skip_upload_returns_none_for_missing_file(self, mock_file_system):
        # The autouse mock_file_system fixture forces os.path.exists()==True;
        # flip just this one test to the real check so we can verify the
        # missing-file guard inside the skip_upload branch.
        mock_file_system["exists"].return_value = False
        uploader = YouTubeUploader(
            credentials_file="/does/not/exist.json",
            token_file="/does/not/exist.json",
            skip_upload=True,
        )
        assert (
            uploader.upload_video(
                video_path="/does/not/exist.mp4",
                title="x",
                description="x",
            )
            is None
        )

    def test_skip_upload_does_not_call_authenticate(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fake_mp4:
            fake_mp4.write(b"x")
            fake_mp4_path = fake_mp4.name

        try:
            uploader = YouTubeUploader(
                credentials_file="/does/not/exist.json",
                token_file="/does/not/exist.json",
                skip_upload=True,
            )

            authenticate_called = []

            def _fail_authenticate() -> bool:
                authenticate_called.append(True)
                return False

            uploader.authenticate = _fail_authenticate

            video_id = uploader.upload_video(
                video_path=fake_mp4_path,
                title="x",
                description="x",
            )
            assert video_id is not None
            assert authenticate_called == []
        finally:
            os.unlink(fake_mp4_path)

    def test_skip_upload_invokes_progress_callback(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fake_mp4:
            fake_mp4.write(b"x")
            fake_mp4_path = fake_mp4.name

        try:
            uploader = YouTubeUploader(
                credentials_file="/does/not/exist.json",
                token_file="/does/not/exist.json",
                skip_upload=True,
            )
            progress_calls = []
            uploader.upload_video(
                video_path=fake_mp4_path,
                title="x",
                description="x",
                on_progress=progress_calls.append,
            )
            assert progress_calls == [100]
        finally:
            os.unlink(fake_mp4_path)
