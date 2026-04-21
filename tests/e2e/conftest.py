"""Local conftest for e2e tests.

The root tests/conftest.py applies several autouse mocks (httpx, filesystem,
av.open) that are great for unit tests but defeat the point of an e2e test.
Override them here so e2e tests can exercise real HTTP mock transports and
real disk I/O.
"""

import pytest


@pytest.fixture(autouse=True)
def mock_httpx():
    """Override the root autouse httpx mock — e2e tests use MockTransport."""
    yield None


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override the root autouse filesystem mock — e2e tests use real tmp_path."""
    yield None


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override the root autouse av mock — e2e tests patch ffmpeg utilities directly."""
    yield None
