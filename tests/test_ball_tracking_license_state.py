"""Tests for video_grouper.ball_tracking.license_state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from video_grouper.ball_tracking import license_state


# Disable root-level autouse fixtures.
@pytest.fixture(autouse=True)
def mock_file_system():
    yield None


@pytest.fixture(autouse=True)
def mock_httpx():
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    yield None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestRecordAndLoad:
    def test_record_persists_state(self, tmp_path: Path):
        expires = _iso(datetime.now(UTC) + timedelta(days=30))
        state = license_state.record(
            tmp_path,
            model_key="ball_detection",
            version="1.0.0",
            tier="premium",
            expires_at=expires,
        )
        assert state.model_key == "ball_detection"
        assert state.version == "1.0.0"
        assert state.tier == "premium"
        assert state.expires_at == expires
        assert state.acquired_at  # ISO string set

        loaded = license_state.load(tmp_path)
        assert loaded == state

    def test_load_returns_none_when_no_file(self, tmp_path: Path):
        assert license_state.load(tmp_path) is None

    def test_record_overwrites_prior_state(self, tmp_path: Path):
        license_state.record(
            tmp_path,
            model_key="ball_detection",
            version="1.0.0",
            tier="free",
            expires_at=_iso(datetime.now(UTC) + timedelta(days=30)),
        )
        license_state.record(
            tmp_path,
            model_key="ball_detection",
            version="1.1.0",
            tier="premium",
            expires_at=_iso(datetime.now(UTC) + timedelta(days=30)),
        )
        loaded = license_state.load(tmp_path)
        assert loaded.version == "1.1.0"
        assert loaded.tier == "premium"

    def test_load_returns_none_on_corrupt_file(self, tmp_path: Path):
        path = tmp_path / "ttt" / "license_state.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        assert license_state.load(tmp_path) is None


class TestStatusLabel:
    def _state(
        self, days_until: float, tier: str = "premium"
    ) -> license_state.LicenseState:
        expires = datetime.now(UTC) + timedelta(days=days_until)
        return license_state.LicenseState(
            model_key="ball_detection",
            version="1.0.0",
            tier=tier,
            expires_at=_iso(expires),
            acquired_at=_iso(datetime.now(UTC)),
        )

    def test_ok_when_far_from_expiry(self):
        state = self._state(days_until=29)  # fresh license
        assert state.status_label().startswith("OK")

    def test_warning_within_5_days_of_expiry(self):
        state = self._state(days_until=3)
        label = state.status_label()
        assert label.startswith("WARNING")
        assert "expires in 3" in label or "expires in 2" in label

    def test_expired(self):
        state = self._state(days_until=-2)
        assert "EXPIRED" in state.status_label()

    def test_tier_appears_in_label(self):
        state = self._state(days_until=29, tier="free")
        assert "free" in state.status_label()


class TestDaysUntilExpiry:
    def test_returns_negative_for_expired(self):
        state = license_state.LicenseState(
            model_key="x",
            version="1",
            tier="free",
            expires_at=_iso(datetime.now(UTC) - timedelta(days=1)),
            acquired_at=_iso(datetime.now(UTC)),
        )
        assert state.days_until_expiry() < 0

    def test_returns_negative_for_unparseable_timestamp(self):
        state = license_state.LicenseState(
            model_key="x",
            version="1",
            tier="free",
            expires_at="garbage",
            acquired_at=_iso(datetime.now(UTC)),
        )
        assert state.days_until_expiry() < 0
