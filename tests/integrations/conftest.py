"""Shared fixtures for the integration-port tests."""

from __future__ import annotations

import pytest

from tests.integrations.expired_ttt import ExpiredTTTSession


@pytest.fixture
def expired_ttt(tmp_path) -> ExpiredTTTSession:
    """See :mod:`tests.integrations.expired_ttt`."""
    return ExpiredTTTSession(str(tmp_path))
