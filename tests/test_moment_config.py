"""Tests for MomentTaggingConfig in the config system."""

import configparser
import tempfile
from pathlib import Path

from video_grouper.utils.config import MomentTaggingConfig, load_config


def _write_config(parser: configparser.ConfigParser) -> Path:
    """Write a ConfigParser to a temp file and return the path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".ini", mode="w", delete=False)
    parser.write(tmp)
    tmp.close()
    return Path(tmp.name)


def _base_parser() -> configparser.ConfigParser:
    """Create a ConfigParser with all required sections."""
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


class TestMomentTaggingConfig:
    def test_defaults_when_section_missing(self):
        """Config without [MOMENT_TAGGING] section should have defaults."""
        parser = _base_parser()
        path = _write_config(parser)
        cfg = load_config(path)
        assert cfg.moment_tagging.enabled is False
        assert cfg.moment_tagging.api_base_url == "http://localhost:8000"
        assert cfg.moment_tagging.service_role_key == ""

    def test_parses_section(self):
        """Config with [MOMENT_TAGGING] section should parse correctly."""
        parser = _base_parser()
        parser.add_section("MOMENT_TAGGING")
        parser.set("MOMENT_TAGGING", "enabled", "true")
        parser.set("MOMENT_TAGGING", "api_base_url", "https://api.example.com")
        parser.set("MOMENT_TAGGING", "service_role_key", "secret123")
        path = _write_config(parser)
        cfg = load_config(path)
        assert cfg.moment_tagging.enabled is True
        assert cfg.moment_tagging.api_base_url == "https://api.example.com"
        assert cfg.moment_tagging.service_role_key == "secret123"

    def test_model_defaults(self):
        """MomentTaggingConfig with no args has sensible defaults."""
        cfg = MomentTaggingConfig()
        assert cfg.enabled is False
        assert cfg.api_base_url == "http://localhost:8000"
        assert cfg.service_role_key == ""
