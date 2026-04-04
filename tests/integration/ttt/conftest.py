"""Fixtures for TTT integration tests.

Overrides root conftest autouse fixtures so real HTTP and filesystem work.
Provides session-scoped authenticated TTTApiClient and Supabase admin client.

Required environment variables (or defaults for local Supabase):
  TTT_API_URL          - TTT backend URL (default: http://localhost:8000)
  TTT_SUPABASE_URL     - Supabase auth URL (default: http://localhost:54321)
  TTT_SUPABASE_ANON_KEY - Supabase anon/publishable key
  TTT_SUPABASE_SERVICE_KEY - Supabase service role key (for admin cleanup)
  TTT_TEST_EMAIL       - Test user email (default: mark.blakley@gmail.com)
  TTT_TEST_PASSWORD    - Test user password (default: password123)
"""

import os

import httpx
import pytest

from video_grouper.api_integrations.ttt_api import TTTApiClient

# ---------------------------------------------------------------------------
# Local TTT environment constants (from env vars or local defaults)
# ---------------------------------------------------------------------------
TTT_API_URL = os.environ.get("TTT_API_URL", "http://localhost:8000")
SUPABASE_URL = os.environ.get("TTT_SUPABASE_URL", "http://localhost:54321")
SUPABASE_ANON_KEY = os.environ.get("TTT_SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("TTT_SUPABASE_SERVICE_KEY", "")
TEST_EMAIL = os.environ.get("TTT_TEST_EMAIL", "mark.blakley@gmail.com")
TEST_PASSWORD = os.environ.get("TTT_TEST_PASSWORD", "password123")

# Seed data UUIDs (from supabase/seed/*.sql)
TEAM_ID = "80000000-0000-0000-0000-000000000001"
CAMERA_ID = "c0000000-0000-0000-0000-000000000001"
CAMERA_MANAGER_ID = "e0000000-0000-0000-0007-000000000001"
GAME_SESSION_ID = "e0000000-0000-0000-0006-000000000001"
PENDING_CLIP_REQUEST_ID = "e0000000-0000-0000-0008-000000000001"
PENDING_PROCESSING_JOB_ID = "e0000000-0000-0000-000b-000000000001"
SERVICE_INSTANCE_ID = "e0000000-0000-0000-000a-000000000001"
MOMENT_TAG_1_ID = "e0000000-0000-0000-000c-000000000001"
MOMENT_TAG_2_ID = "e0000000-0000-0000-000c-000000000002"
USER_ID = "31170262-cbec-460a-a017-94d3ff2c3e9e"
SEED_RECORDING_ID = "d0000000-0000-0000-0000-000000000003"  # pending recording


# ---------------------------------------------------------------------------
# Override root conftest autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_file_system():
    """Override: use real filesystem for TTT integration tests."""
    yield {}


@pytest.fixture(autouse=True)
def mock_ffmpeg():
    """Override: no av.open mocking."""
    yield None


@pytest.fixture(autouse=True)
def mock_httpx():
    """Override: use real httpx."""
    yield None


# ---------------------------------------------------------------------------
# TTT backend fixtures (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ttt_backend_available():
    """Check if TTT backend is running and keys are configured; skip if not."""
    if not SUPABASE_ANON_KEY:
        pytest.skip("TTT_SUPABASE_ANON_KEY not set")
    try:
        resp = httpx.get(f"{TTT_API_URL}/health", timeout=10.0)
        if resp.status_code >= 500:
            pytest.skip("TTT backend unhealthy")
    except Exception as exc:
        pytest.skip(f"TTT backend not available at {TTT_API_URL}: {exc}")


@pytest.fixture(scope="session")
def ttt_client(ttt_backend_available, tmp_path_factory):
    """Session-scoped authenticated TTTApiClient for live TTT tests."""
    storage = str(tmp_path_factory.mktemp("ttt_integration"))
    client = TTTApiClient(
        supabase_url=SUPABASE_URL,
        anon_key=SUPABASE_ANON_KEY,
        api_base_url=TTT_API_URL,
        storage_path=storage,
    )
    client.login(TEST_EMAIL, TEST_PASSWORD)
    yield client
    client._http.close()


@pytest.fixture(scope="session")
def supabase_admin(ttt_backend_available):
    """Admin client for PostgREST -- creates/deletes test data directly.

    Uses the Supabase service role key which bypasses RLS.
    Tables are in the coaching_sessions schema.
    """
    client = httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
            "Accept-Profile": "coaching_sessions",
            "Content-Profile": "coaching_sessions",
        },
        timeout=10.0,
    )
    yield client
    client.close()


def supabase_delete(admin_client: httpx.Client, table: str, record_id: str):
    """Best-effort delete a record via PostgREST. Logs but doesn't raise."""
    try:
        admin_client.delete(f"/{table}?id=eq.{record_id}")
    except Exception:
        pass  # Cleanup failure is not a test failure
