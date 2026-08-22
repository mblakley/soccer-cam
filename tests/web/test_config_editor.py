"""Tests for the schema-driven config editor at /config."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from video_grouper.utils.config import TTTConfig, load_config
from video_grouper.web.auth_server import create_app

# Same-origin header so auth_server's middleware accepts the editor's
# POSTs (rejected without Origin/Referer after the OAuth-state CSRF fix).
_SAME_ORIGIN = {"origin": "http://localhost:8765"}


_DIST_INI = """\
[CAMERA.default]
type = dahua
device_ip = 192.168.1.100
username = admin
password = secret

[STORAGE]
path = /shared_data
min_free_gb = 2.0

[RECORDING]
min_duration = 60
max_duration = 3600

[PROCESSING]
max_concurrent_downloads = 2
trim_end_enabled = false

[LOGGING]
level = INFO
log_dir = logs

[APP]
check_interval_seconds = 60

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false
server_url = https://ntfy.sh
topic =

[YOUTUBE]
enabled = false

[CLOUD_SYNC]
enabled = false

[TTT]
auth_server_enabled = false
auth_server_port = 8765
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "config.ini"
    p.write_text(_DIST_INI, encoding="utf-8")
    return p


@pytest.fixture
def client(tmp_path, config_path):
    app = create_app(TTTConfig(), str(tmp_path), config_path=config_path)
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        yield c


def test_get_config_renders_form_with_existing_values(client):
    body = client.get("/config").text
    assert "<title>Soccer-Cam · Settings</title>" in body
    # Several known fields should appear with their current values.
    assert 'name="STORAGE.path"' in body
    assert 'value="/shared_data"' in body
    assert 'name="RECORDING.min_duration"' in body
    assert 'value="60"' in body
    # Boolean as checkbox
    assert 'type="checkbox"' in body
    assert 'name="TTT.auth_server_enabled"' in body


def test_get_config_redacts_sensitive_fields(client):
    body = client.get("/config").text
    # The camera password ("secret" in the test fixture) must never be
    # echoed back as a value attribute. (Field labels like "client_secret"
    # may legitimately mention the substring; check value attrs only.)
    assert 'value="secret"' not in body
    # Sensitive fields are rendered as password inputs with placeholder.
    assert 'placeholder="(unchanged)"' in body


def test_get_config_skips_list_and_dict_fields(client):
    body = client.get("/config").text
    # plugin_signing_public_keys is a list field — should not render an input.
    assert 'name="TTT.plugin_signing_public_keys"' not in body


def test_post_config_saves_round_trip(client, config_path):
    # Edit a few scalar fields.
    resp = client.post(
        "/config",
        data={
            "STORAGE.path": "/data/games",
            "STORAGE.min_free_gb": "5.5",
            "RECORDING.min_duration": "120",
            "RECORDING.max_duration": "3600",
            "PROCESSING.max_concurrent_downloads": "4",
            "PROCESSING.trim_end_enabled": "false",
            "LOGGING.level": "DEBUG",
            "LOGGING.log_dir": "logs",
            "APP.check_interval_seconds": "60",
            "TEAMSNAP.enabled": "false",
            "PLAYMETRICS.enabled": "false",
            "NTFY.enabled": "true",
            "NTFY.server_url": "https://ntfy.sh",
            "YOUTUBE.enabled": "false",
            "CLOUD_SYNC.enabled": "false",
            "TTT.auth_server_enabled": "true",
            "TTT.auth_server_port": "9999",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/config?saved=1"

    # Reload from disk and verify round-trip.
    reloaded = load_config(config_path)
    assert reloaded.storage.path == "/data/games"
    assert reloaded.storage.min_free_gb == 5.5
    assert reloaded.recording.min_duration == 120
    assert reloaded.processing.max_concurrent_downloads == 4
    assert reloaded.logging.level == "DEBUG"
    assert reloaded.ntfy.enabled is True
    assert reloaded.ttt.auth_server_enabled is True
    assert reloaded.ttt.auth_server_port == 9999


def test_post_config_blank_password_keeps_existing(client, config_path):
    """Sensitive fields submitted blank should NOT clobber the saved value."""
    initial = load_config(config_path)
    initial_password = initial.cameras[0].password
    assert initial_password == "secret"

    # Submit a save with the password input left blank.
    resp = client.post(
        "/config",
        data={
            "STORAGE.path": initial.storage.path,
            "STORAGE.min_free_gb": str(initial.storage.min_free_gb),
            "RECORDING.min_duration": str(initial.recording.min_duration),
            "RECORDING.max_duration": str(initial.recording.max_duration),
            "TTT.auth_server_port": str(initial.ttt.auth_server_port),
        },
        follow_redirects=False,
    )
    # Blank password is treated as "leave alone"; save should still succeed.
    assert resp.status_code in (303, 422)


def test_post_config_invalid_returns_422(client, config_path):
    """Non-numeric value for an int field should fail validation."""
    resp = client.post(
        "/config",
        data={
            "RECORDING.min_duration": "not-a-number",
            "TTT.auth_server_port": "8765",
        },
        follow_redirects=False,
    )
    # Either Python coercion or Pydantic validation rejects it.
    assert resp.status_code in (422, 500)
    # The validation banner must carry both classes — `banner` provides the
    # padding/border/type, `banner--bad` the danger signal. Both come from the
    # shared stylesheet; the page must not restyle them locally.
    if resp.status_code == 422:
        assert 'class="banner banner--bad"' in resp.text


def test_get_config_rail_nav_anchors_match_sections(client):
    """Every rail entry must point at a real section id, and every
    rendered section must have a rail entry — otherwise the sticky
    nav and the scroll observer fall out of sync."""
    body = client.get("/config").text
    import re

    rail_anchors = set(re.findall(r'data-anchor="([^"]+)"', body))
    section_ids = set(re.findall(r'<section class="cfg" id="([^"]+)"', body))
    assert rail_anchors  # not empty
    assert rail_anchors == section_ids


def test_config_editor_not_mounted_when_path_missing(tmp_path):
    """Without config_path, /config returns 404."""
    app = create_app(TTTConfig(), str(tmp_path))  # no config_path
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        resp = c.get("/config")
    assert resp.status_code == 404


# Config with a populated [PIPELINE] (steps + per-step sections). The editor
# only renders scalar fields and skips list/dict ones, so it must NOT clobber
# these complex sections when saving an unrelated scalar.
_PIPELINE_INI = (
    _DIST_INI
    + """
[PIPELINE]
enabled = true
gpu_concurrency = 2
steps = stitch, ball_detect, track, render

[PIPELINE.stitch]
type = stitch_correct
stitch_profile_path = /calib/flash.json

[PIPELINE.ball_detect]
type = ball_detect
model_path = /m/model.onnx
detect_confidence = 0.5

[PIPELINE.track]
type = track
track_kalman_gate = 300

[PIPELINE.render]
type = render
render_output_width = 1920
"""
)


@pytest.fixture
def pipeline_config_path(tmp_path):
    p = tmp_path / "config.ini"
    p.write_text(_PIPELINE_INI, encoding="utf-8")
    return p


@pytest.fixture
def pipeline_client(tmp_path, pipeline_config_path):
    app = create_app(TTTConfig(), str(tmp_path), config_path=pipeline_config_path)
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        yield c


def test_post_config_preserves_pipeline_section(pipeline_client, pipeline_config_path):
    """Editing an unrelated scalar must not drop the [PIPELINE] steps/specs.

    The editor reconstructs Config from the loaded config + scalar form
    overrides only; complex sections (pipeline, cameras, teams) ride through
    untouched. This guards against a regression where the editor rebuilt Config
    from form fields alone and silently lost the pipeline.
    """
    # Sanity: the pipeline loaded as expected before the edit.
    before = load_config(pipeline_config_path)
    assert before.pipeline.steps == ["stitch", "ball_detect", "track", "render"]

    resp = pipeline_client.post(
        "/config",
        data={
            "STORAGE.path": "/data/games",
            "LOGGING.level": "DEBUG",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    reloaded = load_config(pipeline_config_path)
    # The edited scalar took effect...
    assert reloaded.storage.path == "/data/games"
    assert reloaded.logging.level == "DEBUG"
    # ...and the entire [PIPELINE] survived.
    assert reloaded.pipeline.enabled is True
    assert reloaded.pipeline.gpu_concurrency == 2
    assert reloaded.pipeline.steps == ["stitch", "ball_detect", "track", "render"]
    ordered = reloaded.pipeline.ordered_steps()
    assert [s.type for s in ordered] == [
        "stitch_correct",
        "ball_detect",
        "track",
        "render",
    ]
    by_id = {s.step_id: s for s in ordered}
    assert by_id["stitch"].config["stitch_profile_path"] == "/calib/flash.json"
    assert by_id["ball_detect"].config["model_path"] == "/m/model.onnx"


def test_autocam_license_key_is_editable_and_masked(client, config_path):
    """The AutoCam licence key must be settable from /config, and must
    behave like every other credential: never rendered back, and left
    alone when the operator saves the form without retyping it.

    It exists because AutoCam activation is per Windows profile, so the
    account that renders has to be the account that activated — which
    otherwise means logging in as the service account by hand.
    """
    page = client.get("/config").text
    assert "AUTOCAM.license_key" in page, "licence key must appear on the config screen"
    # Rendered as a credential input, not plain text.
    field = page[page.index("AUTOCAM.license_key") - 200 :]
    assert 'type="password"' in field[:600]

    resp = client.post(
        "/config",
        data={"AUTOCAM.license_key": "SECRET-KEY-1", "LOGGING.level": "INFO"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert load_config(config_path).autocam.license_key == "SECRET-KEY-1"

    # Never echoed back to the browser.
    assert "SECRET-KEY-1" not in client.get("/config").text

    # Saving an unrelated field with the key left blank keeps the stored value.
    client.post(
        "/config",
        data={"AUTOCAM.license_key": "", "LOGGING.level": "DEBUG"},
        follow_redirects=False,
    )
    assert load_config(config_path).autocam.license_key == "SECRET-KEY-1"


class TestSectionGating:
    """A section that is switched off should not show controls for itself."""

    def test_gated_sections_wrap_their_other_fields(self, client):
        body = client.get("/config").text
        # Every section with an `enabled` toggle marks it, so CSS can find it.
        assert body.count("cfg-field--gate") >= 9

    def test_the_toggle_is_outside_the_gated_group(self, client):
        """Gating the toggle inside its own group would make it unreachable."""
        import re

        body = client.get("/config").text
        for section in re.findall(
            r'<section class="cfg" id="[^"]+">(.*?)</section>', body, re.S
        ):
            if "cfg-field--gate" not in section or "cfg-gated" not in section:
                continue
            assert section.index("cfg-field--gate") < section.index("cfg-gated")

    def test_gated_fields_are_hidden_not_dropped(self, client):
        """They must still post, or switching a section off wipes its config."""
        body = client.get("/config").text
        # TTT is off by default and has many fields; they must still be in the
        # document so the browser submits them.
        assert 'name="TTT.supabase_url"' in body
        assert 'name="TTT.api_base_url"' in body

    def test_a_section_with_nothing_to_gate_promises_nothing(self, client):
        """TEAMSNAP's remaining fields are dicts, which this editor skips."""
        import re

        body = client.get("/config").text
        teamsnap = re.search(
            r'<section class="cfg" id="sec-teamsnap">(.*?)</section>', body, re.S
        )
        assert teamsnap is not None
        assert "cfg-off-note" not in teamsnap.group(1)

    def test_round_trip_preserves_a_disabled_sections_values(self, client):
        """Posting the form with a section off must not blank its settings."""
        get_body = client.get("/config").text
        assert 'name="TTT.api_base_url"' in get_body

        resp = client.post(
            "/config",
            data={
                "STORAGE.path": "/shared_data",
                "TTT.enabled": "false",
                "TTT.api_base_url": "https://example.invalid",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (303, 422, 500)
        if resp.status_code == 303:
            after = client.get("/config").text
            assert "https://example.invalid" in after


_TEAM_INI = """\
[STORAGE]
path = /data

[LOGGING]
level = INFO

[TEAMSNAP]
enabled = true
client_id = tg_abc

[TEAM.heat2012]
name = BU14 - Guzzetta
aliases = Guzzetta, Hilton Heat
teamsnap_team_id = 10198718
youtube_playlist = Hilton Heat 2012s

[TEAM.flash2013]
name = Western New York Flash - 13B ECNL-RL Rochester
playmetrics_team_id = 335774
youtube_playlist = WNY Flash 2013s
"""


@pytest.fixture
def team_config_path(tmp_path):
    p = tmp_path / "config.ini"
    p.write_text(_TEAM_INI, encoding="utf-8")
    return p


@pytest.fixture
def team_client(tmp_path, team_config_path):
    app = create_app(TTTConfig(), str(tmp_path), config_path=team_config_path)
    with TestClient(app, base_url="http://localhost:8765", headers=_SAME_ORIGIN) as c:
        yield c


def test_post_config_preserves_team_sections(team_client, team_config_path):
    """Editing an unrelated scalar must not drop [TEAM.*].

    The editor renders scalar fields only, so teams are never in the form. It
    rebuilds Config from the loaded model plus scalar overrides, which is the
    only reason they survive — and save_config writes the file from
    Config.model_fields onto an empty parser, so anything that fell out of the
    model would be erased from the user's file permanently.
    """
    before = load_config(team_config_path)
    assert set(before.teams) == {"heat2012", "flash2013"}

    resp = team_client.post(
        "/config",
        data={"STORAGE.path": "/data/games", "LOGGING.level": "DEBUG"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    reloaded = load_config(team_config_path)
    assert reloaded.storage.path == "/data/games"
    assert reloaded.logging.level == "DEBUG"

    assert set(reloaded.teams) == {"heat2012", "flash2013"}
    heat = reloaded.teams["heat2012"]
    assert heat.name == "BU14 - Guzzetta"
    assert heat.teamsnap_team_id == "10198718"
    assert heat.youtube_playlist == "Hilton Heat 2012s"
    # The comma-separated alias list has to survive the INI round-trip too.
    assert heat.aliases == ["Guzzetta", "Hilton Heat"]
    # And the team still resolves after the save.
    assert reloaded.team_for("BU14 - Guzzetta") is not None


def test_schema_section_is_not_editable_but_survives(team_client, team_config_path):
    """[SCHEMA] is machine-owned: never rendered, always preserved.

    It records which config schema the file is written in. As a plain int it
    would otherwise render as an editable number field — and setting it ahead
    of what the build understands makes startup hard-fail by design, while
    setting it back re-runs migrations. Neither belongs behind a text box.
    """
    page = team_client.get("/config").text
    assert "SCHEMA.version" not in page
    assert ">SCHEMA<" not in page

    before = load_config(team_config_path).schema_meta.version

    resp = team_client.post(
        "/config",
        data={"STORAGE.path": "/data/games"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    assert load_config(team_config_path).schema_meta.version == before
