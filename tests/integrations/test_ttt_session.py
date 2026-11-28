"""TTTApiClient.ensure_signed_in: the one session gate every TTT port uses.

An expired access token with a refresh token still held is a session, not
"signed out": the gate refreshes it. It never raises, backs off after a failed
refresh (offline at the field), and refreshes once however many threads ask.
"""

import threading
import time

import httpx

import video_grouper.api_integrations.ttt_api as ttt_api
from tests.integrations.expired_ttt import ExpiredTTTSession, make_jwt


def test_a_valid_token_is_signed_in_without_any_request(expired_ttt):
    expired_ttt.client._access_token = make_jwt(time.time() + 3600)
    expired_ttt.client._expires_at = time.time() + 3600
    assert expired_ttt.client.ensure_signed_in() is True
    assert expired_ttt.requests == []


def test_an_expired_token_is_refreshed(expired_ttt):
    client = expired_ttt.client
    assert client.is_authenticated() is False
    assert client.ensure_signed_in() is True
    assert expired_ttt.refreshes == 1
    assert client.is_authenticated() is True
    assert client._refresh_token_value == "refresh-2"
    assert client.ensure_signed_in() is True  # fresh now: no second refresh
    assert expired_ttt.refreshes == 1


def test_a_failed_refresh_backs_off_then_retries(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ttt_api.time, "monotonic", lambda: clock[0])
    session = ExpiredTTTSession(str(tmp_path), refresh_ok=False)
    client = session.client

    assert client.ensure_signed_in() is False
    assert session.refreshes == 1
    clock[0] += client.REFRESH_BACKOFF_SECONDS - 1
    assert client.ensure_signed_in() is False
    assert session.refreshes == 1  # still backing off: no request

    clock[0] += 2
    session.refresh_ok = True
    assert client.ensure_signed_in() is True
    assert session.refreshes == 2


def test_offline_never_raises(tmp_path):
    session = ExpiredTTTSession(str(tmp_path))

    def offline(request):
        raise httpx.ConnectError("no route to host", request=request)

    session.client._http = httpx.Client(transport=httpx.MockTransport(offline))
    assert session.client.ensure_signed_in() is False


def test_no_refresh_token_means_signed_out_without_a_request(expired_ttt):
    expired_ttt.client._refresh_token_value = None
    assert expired_ttt.client.ensure_signed_in() is False
    assert expired_ttt.client.has_session() is False
    assert expired_ttt.requests == []


def test_no_session_at_all(expired_ttt):
    client = expired_ttt.client
    client._access_token = client._refresh_token_value = client._expires_at = None
    assert client.ensure_signed_in() is False
    assert client.has_session() is False
    assert expired_ttt.requests == []


def test_an_oauth_session_has_no_refresh_token(expired_ttt):
    client = expired_ttt.client
    client.set_session_from_token(make_jwt(time.time() + 3600))
    assert client.ensure_signed_in() is True
    # Expired: nothing to refresh with -> signed out, no request made.
    client._expires_at = time.time() - 1
    assert client.ensure_signed_in() is False
    assert expired_ttt.requests == []


def test_a_new_sign_in_ends_the_backoff(tmp_path):
    session = ExpiredTTTSession(str(tmp_path), refresh_ok=False)
    client = session.client
    assert client.ensure_signed_in() is False
    client.set_session_from_token(make_jwt(time.time() + 3600))
    assert client._refresh_retry_at == 0.0
    assert client.ensure_signed_in() is True


def test_has_session_counts_a_refreshable_expired_token(expired_ttt):
    assert expired_ttt.client.is_authenticated() is False
    assert expired_ttt.client.has_session() is True


def test_concurrent_callers_refresh_once(expired_ttt):
    client = expired_ttt.client
    results: list[bool] = []
    start = threading.Barrier(8)

    def call():
        start.wait()
        results.append(client.ensure_signed_in())

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 8
    assert expired_ttt.refreshes == 1  # a refresh token is spent once


def test_requests_and_the_gate_share_one_refresh(expired_ttt):
    """``_request`` refreshes under the same lock, so a port's call right after
    the gate doesn't refresh again."""
    expired_ttt.routes[("GET", "/api/device-link/me")] = []
    assert expired_ttt.client.ensure_signed_in() is True
    assert expired_ttt.client.get_team_assignments() == []
    assert expired_ttt.refreshes == 1
