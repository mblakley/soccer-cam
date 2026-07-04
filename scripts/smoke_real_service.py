"""Real-service end-to-end smoke test for the moment-tagger pipeline.

Unlike the other smoke scripts which call processors in-process, this one
runs ``video_grouper`` as an actual subprocess and lets its own polling
loops handle every step. The test drives moment-tagger UI in a real
headless browser, then waits for the real soccer-cam service to do the
rest.

Pipeline under test (everything below the dashes runs in the soccer-cam
subprocess; the test only POSTs to TTT, taps the UI, and verifies DB
state)::

    moment-tagger UI taps
        --> POST /api/moment-tags        (TTT route)
        --
        --> ClipDiscoveryProcessor.discover_work
              -> compute_combined_offset / compute_trimmed_offset
              -> PATCH /api/moment-tags/{id}   (video_offset_seconds)
              -> POST /api/moment-clips
        --
    POST /api/games/{id}/videos  (TTT route, requires the
                                  GameVideos.created_at fix)
        -> auto_create_moment_tagger_reels  (Phase A hook)
            -> highlight_reels row source='moment_tagger', status='pending'
        --
        --> HighlightReelProcessor.discover_work
              -> claim_highlight
              -> trim each moment_clip from local combined.mp4
              -> HighlightCompilationTask.execute  (FFmpeg concat)
              -> YouTubeUploader.upload_video (skip_upload returns fake id)
              -> PATCH /api/highlights/{id} status='ready'  (Phase D
                                                              notifications)

Requires:
    - TTT docker stack at localhost:8000 (with the GameVideos.created_at
      fix applied to the live backend image).
    - Local Supabase at 127.0.0.1:54322.
    - FFmpeg + ffprobe on PATH.
    - Playwright (chromium installed).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg2
import psycopg2.extras
from playwright.sync_api import sync_playwright

TTT_BASE = "http://localhost:8000"
FRONTEND_BASE = "http://localhost:3000"
SUPABASE_URL = "http://127.0.0.1:54321"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub2"
    "4iLCJleHAiOjE5ODM4MTI5OTZ9.CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
)
SUPABASE_SERVICE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcn"
    "ZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)
DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

ROOT_TMP = Path("C:/tmp/soccer-cam-real-smoke").resolve()
STORAGE_DIR = ROOT_TMP / "storage"
CONFIG_PATH = ROOT_TMP / "config.ini"
LOG_PATH = ROOT_TMP / "soccer-cam.log"

SEED_USER_ID = "31170262-cbec-460a-a017-94d3ff2c3e9e"
SEED_TEAM_ID = "80000000-0000-0000-0000-000000000001"
SEED_PLAYER_ID = "e0000000-0000-0000-0001-000000000001"
SEED_CAMERA_ID = "c0000000-0000-0000-0000-000000000099"
SMOKE_MARKER = "smoke-real-"

# Color schedule: one color per minute of the 10-minute test video.
# Expected RGB at the midpoint of each minute. The smoke generates the video
# with these colors then re-samples the rendered output to verify trim+concat
# preserved the source content.
COLOR_SCHEDULE: list[tuple[str, tuple[int, int, int]]] = [
    ("red", (255, 0, 0)),
    ("orange", (255, 128, 0)),
    ("yellow", (255, 255, 0)),
    ("green", (0, 255, 0)),
    ("cyan", (0, 255, 255)),
    ("blue", (0, 0, 255)),
    ("magenta", (255, 0, 255)),
    ("white", (255, 255, 255)),
    ("brown", (139, 69, 19)),
    ("black", (0, 0, 0)),
]


# ---------------------------------------------------------------------------
# Small utility helpers
# ---------------------------------------------------------------------------


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}", flush=True)


def http_post(path: str, body: dict, token: str | None = None) -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(TTT_BASE + path, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {path} -> {e.code}: {body_txt}") from None


def db_exec(sql: str, params: tuple = ()) -> list[dict]:
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = list(cur.fetchall()) if cur.description else []
            conn.commit()
            return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def preflight() -> None:
    banner("Pre-flight: verify TTT, Postgres, FFmpeg")
    # TTT
    try:
        with urlopen(TTT_BASE + "/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health.get("status") == "healthy", health
    except Exception as exc:
        raise RuntimeError(f"TTT not reachable at {TTT_BASE}/health: {exc}") from None
    print(f"  [ok] TTT healthy at {TTT_BASE}")

    # Postgres
    conn = psycopg2.connect(DB_URL)
    conn.close()
    print(f"  [ok] Postgres reachable at {DB_URL}")

    # ffmpeg + ffprobe
    for tool in ("ffmpeg", "ffprobe"):
        out = subprocess.run([tool, "-version"], capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"{tool} not on PATH")
    print("  [ok] FFmpeg + ffprobe on PATH")


# ---------------------------------------------------------------------------
# Color test video
# ---------------------------------------------------------------------------


def _build_color_filter() -> str:
    """Build an FFmpeg lavfi filter expression that paints one color per minute.

    Each minute of the 10-minute video gets its own constant color from
    COLOR_SCHEDULE. We use a chained 'drawbox' over a black background since
    'color' source only takes one color. drawbox covers the full frame and
    forces a fresh fill on every frame for the configured window.
    """
    # Build an expression series: at t=[k*60, (k+1)*60) draw the k-th color.
    parts: list[str] = []
    parts.append("color=c=black:s=320x240:r=25:d=600")
    for idx, (_, (r, g, b)) in enumerate(COLOR_SCHEDULE):
        start = idx * 60
        end = (idx + 1) * 60
        parts.append(
            f"drawbox=x=0:y=0:w=320:h=240:color=0x{r:02x}{g:02x}{b:02x}:"
            f"t=fill:enable='between(t,{start},{end - 0.001})'"
        )
    return ",".join(parts)


def generate_test_video(out_path: Path) -> None:
    if out_path.exists() and out_path.stat().st_size > 100_000:
        print(f"  [skip] color test video already exists: {out_path}")
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = _build_color_filter()
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=25:d=600",
        "-vf",
        vf.split(",", 1)[1],
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-g",
        "25",
        "-keyint_min",
        "25",
        "-force_key_frames",
        "expr:gte(t,n_forced*1)",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out_path),
    ]
    print(f"  [ffmpeg] generating {out_path} ({len(COLOR_SCHEDULE)}-color, 10min)")
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  [ok] generated {out_path} ({size_mb:.1f} MB)")


# ---------------------------------------------------------------------------
# DB teardown + seed
# ---------------------------------------------------------------------------


def teardown_storage() -> None:
    """Remove every smoke storage dir from previous runs so the
    ClipDiscoveryProcessor doesn't pick up stale state files."""
    if not STORAGE_DIR.exists():
        return
    import shutil

    removed = 0
    for entry in STORAGE_DIR.iterdir():
        if entry.is_dir() and entry.name.startswith(SMOKE_MARKER):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        print(f"  [ok] removed {removed} stale smoke storage dirs")


def teardown_prior_smoke_rows() -> None:
    banner("Teardown: remove rows + storage from prior smoke runs")
    teardown_storage()
    # Delete in FK dependency order; smoke marker lives on game_sessions
    # (recording_group_dir) and on highlight_reels (title), so we identify
    # smoke games via game_sessions.recording_group_dir prefix.
    smoke_session_ids = [
        r["id"]
        for r in db_exec(
            "SELECT id FROM coaching_sessions.game_sessions "
            "WHERE recording_group_dir LIKE %s",
            (SMOKE_MARKER + "%",),
        )
    ]
    smoke_game_ids: list[str] = []
    if smoke_session_ids:
        smoke_game_ids = [
            r["game_id"]
            for r in db_exec(
                # Post-unify-videos: youtube_video_id moved off game_videos
                # to media.videos (via the new junction). Join through it.
                "SELECT DISTINCT cr.game_session_id, g.id AS game_id "
                "FROM coaching_sessions.camera_recordings cr "
                "JOIN media.videos mv ON mv.source_url LIKE '%%' || cr.youtube_video_id || '%%' "
                "JOIN game_analysis.game_videos gv ON gv.video_id = mv.id "
                "JOIN game_analysis.games g ON g.id = gv.game_id "
                "WHERE cr.game_session_id = ANY(%s::uuid[])",
                (smoke_session_ids,),
            )
            if r.get("game_id")
        ]

    # Highlight reel cascade
    db_exec(
        "DELETE FROM coaching_sessions.highlight_reel_clips "
        "WHERE highlight_reel_id IN ("
        "SELECT id FROM coaching_sessions.highlight_reels WHERE title LIKE %s)",
        (SMOKE_MARKER + "%",),
    )
    db_exec(
        "DELETE FROM coaching_sessions.notifications "
        "WHERE payload->>'reel_id' IN ("
        "SELECT id::text FROM coaching_sessions.highlight_reels WHERE title LIKE %s)",
        (SMOKE_MARKER + "%",),
    )
    db_exec(
        "DELETE FROM coaching_sessions.highlight_reels WHERE title LIKE %s",
        (SMOKE_MARKER + "%",),
    )

    # Moment cascade keyed off smoke sessions
    if smoke_session_ids:
        db_exec(
            "DELETE FROM coaching_sessions.moment_clips "
            "WHERE game_session_id = ANY(%s::uuid[])",
            (smoke_session_ids,),
        )
        db_exec(
            "DELETE FROM coaching_sessions.moment_tags "
            "WHERE game_session_id = ANY(%s::uuid[])",
            (smoke_session_ids,),
        )
        db_exec(
            "DELETE FROM coaching_sessions.camera_recordings "
            "WHERE game_session_id = ANY(%s::uuid[])",
            (smoke_session_ids,),
        )
        db_exec(
            "DELETE FROM coaching_sessions.game_sessions WHERE id = ANY(%s::uuid[])",
            (smoke_session_ids,),
        )

    if smoke_game_ids:
        db_exec(
            "DELETE FROM game_analysis.game_videos WHERE game_id = ANY(%s::uuid[])",
            (smoke_game_ids,),
        )
        db_exec(
            "DELETE FROM game_analysis.games WHERE id = ANY(%s::uuid[])",
            (smoke_game_ids,),
        )

    # Smoke cameras: keyed off SEED_CAMERA_ID + smoke marker on name
    db_exec(
        "DELETE FROM coaching_sessions.cameras WHERE name LIKE %s",
        (SMOKE_MARKER + "%",),
    )

    # Smoke player rows we may have stamped (don't blow away real seed data)
    db_exec(
        "DELETE FROM coaching_sessions.players WHERE name LIKE %s",
        (SMOKE_MARKER + "%",),
    )
    print(
        f"  [ok] cleaned {len(smoke_session_ids)} smoke sessions / {len(smoke_game_ids)} games"
    )


def ensure_seed_player() -> str:
    row = db_exec(
        "SELECT id FROM coaching_sessions.players WHERE id = %s",
        (SEED_PLAYER_ID,),
    )
    if row:
        return SEED_PLAYER_ID
    db_exec(
        "INSERT INTO coaching_sessions.players (id, parent_user_id, team_id, name) "
        "VALUES (%s, %s, %s, %s)",
        (SEED_PLAYER_ID, SEED_USER_ID, SEED_TEAM_ID, SMOKE_MARKER + "Player"),
    )
    return SEED_PLAYER_ID


def ensure_seed_camera_manager() -> None:
    """The worker PATCH /api/internal/moment-tags/{id} authorizes the caller
    as a camera_manager for the tag's team. Seed that row so SEED_USER_ID's
    user-JWT-authed requests are accepted by the TTT service layer."""
    row = db_exec(
        "SELECT id FROM coaching_sessions.camera_managers "
        "WHERE user_id = %s AND team_id = %s",
        (SEED_USER_ID, SEED_TEAM_ID),
    )
    if row:
        return
    db_exec(
        "INSERT INTO coaching_sessions.camera_managers "
        "(id, team_id, user_id, email, name, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            str(uuid.uuid4()),
            SEED_TEAM_ID,
            SEED_USER_ID,
            "mark.blakley@gmail.com",
            SMOKE_MARKER + "CameraManager",
            SEED_USER_ID,
        ),
    )


def seed_db(recording_dir_name: str, recording_start: datetime) -> dict[str, str]:
    banner("Seed DB: game + game_session + camera + camera_recording")
    game_id = str(uuid.uuid4())
    gs_id = str(uuid.uuid4())
    cam_id = SEED_CAMERA_ID
    cam_rec_id = str(uuid.uuid4())
    # Real YouTube IDs are exactly 11 chars [a-zA-Z0-9_-] — TTT's
    # camera-recording auto-link regex requires exactly that shape.
    # Length: 6 ("smoke-") + 5 hex = 11. Prefix lets teardown find this row.
    yt_id = f"smoke-{uuid.uuid4().hex[:5]}"

    db_exec(
        "INSERT INTO game_analysis.games "
        "(id, home_team_id, away_team_id, date, game_type, opponent_name, created_by) "
        "VALUES (%s, %s, %s, CURRENT_DATE, 'friendly', %s, %s)",
        (game_id, SEED_TEAM_ID, SEED_TEAM_ID, SMOKE_MARKER + "Opponent", SEED_USER_ID),
    )
    db_exec(
        "INSERT INTO coaching_sessions.game_sessions "
        "(id, team_id, recording_group_dir, status, game_date, opponent_name, "
        "recording_start_time, created_by, sync_status) "
        "VALUES (%s, %s, %s, 'live', CURRENT_DATE, %s, %s, %s, 'pending')",
        (
            gs_id,
            SEED_TEAM_ID,
            recording_dir_name,
            SMOKE_MARKER + "Opponent",
            recording_start,
            SEED_USER_ID,
        ),
    )

    # Camera row: idempotent — UPSERT on the fixed SEED_CAMERA_ID so reruns
    # don't accumulate "(unique violation)" failures.
    db_exec(
        "INSERT INTO coaching_sessions.cameras (id, team_id, user_id, name) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        (cam_id, SEED_TEAM_ID, SEED_USER_ID, SMOKE_MARKER + "Camera"),
    )

    # Camera recording: links the youtube_video_id we'll later POST to TTT.
    db_exec(
        "INSERT INTO coaching_sessions.camera_recordings "
        "(id, camera_id, team_id, game_session_id, file_name, recording_start, "
        "youtube_video_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            cam_rec_id,
            cam_id,
            SEED_TEAM_ID,
            gs_id,
            "combined.mp4",
            recording_start,
            yt_id,
        ),
    )

    ensure_seed_player()
    ensure_seed_camera_manager()

    print(f"  game={game_id} session={gs_id} camera={cam_id} yt_id={yt_id}")
    return {
        "game_id": game_id,
        "game_session_id": gs_id,
        "camera_id": cam_id,
        "camera_recording_id": cam_rec_id,
        "youtube_video_id": yt_id,
    }


# ---------------------------------------------------------------------------
# Storage tree (state.json + match_info.ini + combined.mp4 + trimmed file)
# ---------------------------------------------------------------------------


def build_storage_tree(
    recording_dir_name: str,
    recording_start: datetime,
    combined_mp4_source: Path,
) -> Path:
    banner("Build storage tree the service expects")
    group_dir = STORAGE_DIR / recording_dir_name
    group_dir.mkdir(parents=True, exist_ok=True)

    combined_dst = group_dir / "combined.mp4"
    if not combined_dst.exists():
        # Hardlink the source video so we don't waste disk on a copy. Falls
        # back to a copy on FS-level errors (e.g. cross-volume).
        try:
            os.link(combined_mp4_source, combined_dst)
        except OSError:
            import shutil

            shutil.copy2(combined_mp4_source, combined_dst)

    # state.json: status=trimmed + one recording file spanning the whole video.
    state = {
        "status": "trimmed",
        "error_message": None,
        "files": {
            "/cam/combined.mp4": {
                "task_type": "recording_file",
                "file_path": "/cam/combined.mp4",
                "start_time": recording_start.isoformat(),
                "end_time": (recording_start + timedelta(seconds=600)).isoformat(),
                "status": "downloaded",
                "metadata": {},
                "skip": False,
                "screenshot_path": None,
                "group_dir": str(group_dir),
                "last_updated": datetime.now(UTC).isoformat(),
                "error_message": None,
            }
        },
    }
    (group_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    # match_info.ini: trim start = 00:00:00 so trimmed = combined.
    match_ini = (
        "[MATCH]\n"
        "my_team_name = SmokeHome\n"
        "opponent_team_name = SmokeAway\n"
        "location = Field\n"
        "start_time_offset = 00:00:00\n"
        "total_duration = 600\n"
    )
    (group_dir / "match_info.ini").write_text(match_ini, encoding="utf-8")

    # The trimmed video lives under the generated subdir layout (see
    # get_trimmed_video_path). Hard-link the same file there so the
    # discovery processor finds it.
    date_part = recording_dir_name.split("-")[0]
    subdir_name = f"{date_part} - SmokeHome vs SmokeAway (Field)"
    subdir = group_dir / subdir_name
    subdir.mkdir(parents=True, exist_ok=True)
    raw_filename = (
        f"smokehome-smokeaway-field-{recording_start.strftime('%m-%d-%Y')}-raw.mp4"
    )
    trimmed_dst = subdir / raw_filename
    if not trimmed_dst.exists():
        try:
            os.link(combined_dst, trimmed_dst)
        except OSError:
            import shutil

            shutil.copy2(combined_dst, trimmed_dst)
    print(f"  [ok] storage tree at {group_dir}")
    return group_dir


# ---------------------------------------------------------------------------
# soccer-cam config + subprocess
# ---------------------------------------------------------------------------


def build_config(jwt: str) -> None:
    """Write a minimal config.ini that turns on [TTT] + [MOMENT_TAGGING] +
    [YOUTUBE] skip_upload. Camera config is a no-op simulator so the camera
    poller doesn't try to talk to real hardware."""
    banner("Write soccer-cam config.ini")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_ini = f"""[CAMERA.smoke]
type = dahua
device_ip = 127.0.0.1
username = smoke
password = smoke

[STORAGE]
path = {STORAGE_DIR}

[RECORDING]
min_duration = 60
max_duration = 3600

[PROCESSING]
max_concurrent_downloads = 1
max_concurrent_conversions = 1
retry_attempts = 1
retry_delay = 30

[LOGGING]
level = DEBUG
log_dir = {ROOT_TMP / "logs"}
app_name = video_grouper_smoke
backup_count = 1

[APP]
check_interval_seconds = 5
timezone = America/New_York

[TEAMSNAP]
enabled = false

[PLAYMETRICS]
enabled = false

[NTFY]
enabled = false

[YOUTUBE]
enabled = true
privacy_status = unlisted
skip_upload = true

[BALL_TRACKING]
enabled = false

[TTT]
enabled = true
supabase_url = {SUPABASE_URL}
anon_key = {SUPABASE_ANON_KEY}
api_base_url = {TTT_BASE}
clip_request_poll_interval = 5
camera_id = {SEED_CAMERA_ID}
auth_server_enabled = false

[MOMENT_TAGGING]
enabled = true
api_base_url = {TTT_BASE}
# Field is named `service_role_key` for config-schema backwards compatibility,
# but the actual value is the camera-manager's TTT user JWT. The TTT worker
# route under /api/internal/moment-tags/{id} authenticates as a user and
# authorizes via the camera_managers table.
service_role_key = {jwt}
"""
    CONFIG_PATH.write_text(config_ini, encoding="utf-8")

    # Pre-seed the TTT token cache so the subprocess is authenticated as our
    # dev user without needing email/password in the config.
    token_dir = STORAGE_DIR / "ttt"
    token_dir.mkdir(parents=True, exist_ok=True)
    tokens = {
        "access_token": jwt,
        "refresh_token": None,
        # Make it look like it expires in 6 hours so the client won't try to refresh.
        "expires_at": time.time() + 6 * 3600,
    }
    (token_dir / "tokens.json").write_text(json.dumps(tokens), encoding="utf-8")
    print(f"  [ok] wrote {CONFIG_PATH} + ttt/tokens.json")


def start_soccer_cam() -> subprocess.Popen:
    banner("Start soccer-cam as a real subprocess")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_PATH, "wb")
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        ["uv", "run", "python", "run.py", "--config", str(CONFIG_PATH)],
        cwd=str(repo_root),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    print(f"  [ok] pid={proc.pid} log={LOG_PATH}")
    return proc


def tail_log(needle: str, timeout: float) -> bool:
    """Wait for ``needle`` to appear anywhere in the log file."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            data = ""
        if needle in data:
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Auth + UI driving
# ---------------------------------------------------------------------------


def dev_login() -> str:
    """Get a JWT for SEED_USER_ID via TTT's /api/dev/login route."""
    auth = http_post(
        "/api/dev/login",
        {"user_id": SEED_USER_ID, "email": "mark.blakley@gmail.com"},
    )
    return auth["access_token"]


def drive_moment_tagger_ui() -> int:
    banner("Drive moment-tagger UI in a real headless browser")
    captured: list[Any] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 390, "height": 844})
            page = ctx.new_page()
            page.on(
                "request",
                lambda r: (
                    captured.append({"url": r.url, "method": r.method})
                    if "/api/moment-tags" in r.url and r.method == "POST"
                    else None
                ),
            )

            page.goto(f"{FRONTEND_BASE}/tools?signin=1", wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            page.locator(
                'button:has-text("Dev"), button:has-text("dev login")'
            ).first.click()
            page.wait_for_timeout(2500)

            page.goto(f"{FRONTEND_BASE}/moment-tagger/games", wait_until="networkidle")
            page.wait_for_timeout(1500)

            card = page.locator(f'text="{SMOKE_MARKER}Opponent"').first
            card.wait_for(state="visible", timeout=8000)
            card.click()
            page.wait_for_timeout(1500)

            for i, label in enumerate(["Goal", "Save", "Assist"]):
                lbl = page.locator(f'button:has-text("{label}")').first
                lbl.wait_for(state="visible", timeout=5000)
                lbl.click()
                page.wait_for_timeout(300)
                page.locator('button:has-text("TAG IT")').first.click()
                page.wait_for_timeout(800)
                print(f"  [ok] tap {i + 1}: {label}")

            page.wait_for_timeout(2000)
        finally:
            browser.close()
    return len(captured)


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def poll_until(query_fn, predicate, timeout: float, label: str) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = query_fn()
        if predicate(last):
            return last
        time.sleep(2)
    raise TimeoutError(
        f"{label} did not reach desired state within {timeout}s. Last value: {last}"
    )


def sample_dominant_rgb(
    video_path: Path, timestamp_seconds: float
) -> tuple[int, int, int]:
    """Extract a single frame at the given timestamp and return its mean RGB."""
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{timestamp_seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=8:8",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, check=True)
    pixels = res.stdout
    n = len(pixels) // 3
    if n == 0:
        raise RuntimeError(
            f"no frame extracted from {video_path} at {timestamp_seconds}s"
        )
    r = sum(pixels[i * 3] for i in range(n)) // n
    g = sum(pixels[i * 3 + 1] for i in range(n)) // n
    b = sum(pixels[i * 3 + 2] for i in range(n)) // n
    return (r, g, b)


def manhattan(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--skip-ui",
        action="store_true",
        help="Skip the Playwright UI drive (useful when the moment-tagger "
        "dev-login flow is intermittent). The smoke will instead POST the "
        "tags directly via TTT's API, which leaves video_offset_seconds "
        "unset — the soccer-cam ClipDiscoveryProcessor still computes it.",
    )
    args = ap.parse_args()

    preflight()

    # Recording start: ~10 minutes ago. Tags taken now will fall at offsets
    # roughly 9-10 minutes in (we don't depend on this exactly since
    # video_offset_seconds is computed by the service from wall-clock).
    recording_start = datetime.now(UTC) - timedelta(minutes=10)
    recording_dir_name = SMOKE_MARKER + recording_start.astimezone().strftime(
        "%Y.%m.%d-%H.%M.%S"
    )

    test_video = ROOT_TMP / "test-video.mp4"
    generate_test_video(test_video)

    teardown_prior_smoke_rows()

    state = seed_db(recording_dir_name, recording_start)
    build_storage_tree(recording_dir_name, recording_start, test_video)

    jwt = dev_login()
    build_config(jwt)

    proc = start_soccer_cam()
    try:
        # Tag taps. Time them carefully so they fall within the recording.
        if not args.skip_ui:
            num_taps = drive_moment_tagger_ui()
            print(f"  captured {num_taps} POST /api/moment-tags calls")
            assert num_taps >= 3, f"expected >=3 taps, got {num_taps}"
        else:
            # Direct API path: stamp 3 moment_tags via the route. tagged_at
            # offsets are centered within distinct color-minute bands so the
            # generated 30-second clips don't straddle a color boundary in
            # the source video. clip_window = [tag - 15, tag + 15] => for
            # tag=90 the window is [75,105] (minute 1 = orange), tag=270 =>
            # [255,285] (minute 4 = cyan), tag=450 => [435,465] (minute 7 =
            # white). Each clip's midpoint lands cleanly in one color band.
            tap_offsets_seconds = [90.0, 270.0, 450.0]
            for off in tap_offsets_seconds:
                ts = recording_start + timedelta(seconds=off)
                http_post(
                    "/api/moment-tags",
                    {
                        "game_session_id": state["game_session_id"],
                        "player_id": SEED_PLAYER_ID,
                        "tagged_at": ts.isoformat(),
                        "label": f"smoke-{int(off)}s",
                    },
                    token=jwt,
                )
            print(f"  posted 3 moment_tags directly at {tap_offsets_seconds}")

        banner("Wait for ClipDiscoveryProcessor: PATCH offsets + create clips")
        # Proof that the real ClipDiscoveryProcessor handled the tags
        # end-to-end:
        #   1. video_offset_seconds is non-null on each tag (proves the
        #      worker PATCH /api/internal/moment-tags/{id} succeeded — if
        #      this is still NULL, soccer-cam couldn't write the computed
        #      offset back, which is exactly the bug that motivated the
        #      /api/internal split).
        #   2. a moment_clips row exists per tag (proves POST /api/moment-clips
        #      succeeded after the offset compute).
        # Together: no SQL workaround, no simulation. The processor poll
        # cycle naturally settles once offsets are written (no more pending
        # tags = no re-processing = no duplicate clips).
        tags = poll_until(
            lambda: db_exec(
                "SELECT id::text, video_offset_seconds "
                "FROM coaching_sessions.moment_tags "
                "WHERE game_session_id = %s ORDER BY tagged_at",
                (state["game_session_id"],),
            ),
            lambda rows: (
                len(rows) >= 3
                and all(r.get("video_offset_seconds") is not None for r in rows)
            ),
            timeout=120,
            label="moment_tags.video_offset_seconds written by worker PATCH",
        )
        print(
            "  [ok] {} tags, offsets={}".format(
                len(tags), [round(t["video_offset_seconds"], 2) for t in tags]
            )
        )
        clips = db_exec(
            "SELECT id::text, moment_tag_id::text, clip_start_offset, clip_end_offset "
            "FROM coaching_sessions.moment_clips "
            "WHERE game_session_id = %s ORDER BY clip_start_offset",
            (state["game_session_id"],),
        )
        assert len(clips) == 3, (
            f"expected exactly 3 moment_clips (one per tag) — got {len(clips)}. "
            "Duplicates would indicate the worker PATCH didn't clear the "
            "pending_offset state and the processor re-ran on the same tags."
        )
        # Each tag should have exactly one clip — no duplicates.
        clip_tag_ids = {c["moment_tag_id"] for c in clips}
        assert len(clip_tag_ids) == 3, (
            f"expected 3 unique moment_tag_ids in clips, got {len(clip_tag_ids)}"
        )
        print(f"  [ok] {len(clips)} moment_clips, one per tag (no duplicates)")
        assert tail_log("CLIP_DISCOVERY: Found", timeout=5), (
            "expected CLIP_DISCOVERY log line, log=" + str(LOG_PATH)
        )

        banner("POST /api/games/{id}/videos — auto-create-reel hook (the fix)")
        # Post-unify-videos contract: source_url + video_type only.
        # youtube_video_id is no longer a payload field; the backend extracts
        # it from source_url for the camera-recording auto-link lookup.
        post_body = {
            "source_url": f"https://youtu.be/{state['youtube_video_id']}",
            "video_type": "full",
        }
        post_resp = http_post(
            f"/api/games/{state['game_id']}/videos", post_body, token=jwt
        )
        assert post_resp.get("id"), f"POST returned no id: {post_resp}"
        print(
            f"  [ok] game_video={post_resp['id']} created_at={post_resp.get('created_at')}"
        )

        # Rename the reel so teardown-by-marker can find it.
        db_exec(
            "UPDATE coaching_sessions.highlight_reels SET title = %s "
            "WHERE game_id = %s AND source = 'moment_tagger'",
            (SMOKE_MARKER + "Reel", state["game_id"]),
        )

        banner("Wait for HighlightReelProcessor: status=ready")
        reel = poll_until(
            lambda: (
                db_exec(
                    "SELECT id::text, status, youtube_video_id, file_path, "
                    "error_message FROM coaching_sessions.highlight_reels "
                    "WHERE game_id = %s AND source = 'moment_tagger'",
                    (state["game_id"],),
                )
                or [{}]
            )[0],
            lambda r: r.get("status") in ("ready", "failed"),
            timeout=240,
            label="HighlightReel status=ready",
        )
        assert reel["status"] == "ready", (
            f"reel did not reach ready (status={reel.get('status')}, "
            f"error={reel.get('error_message')}). See {LOG_PATH}"
        )
        print(
            f"  [ok] reel ready: yt={reel['youtube_video_id']} file={reel['file_path']}"
        )
        assert reel["youtube_video_id"].startswith("smoke-"), (
            f"expected smoke-prefixed fake youtube id, got {reel['youtube_video_id']}"
        )

        banner("Verify the rendered mp4 + per-clip color sampling")
        final_path = Path(reel["file_path"])
        assert final_path.exists(), f"rendered mp4 missing: {final_path}"

        sorted_clips = sorted(clips, key=lambda c: c["clip_start_offset"])
        # Each clip in the rendered video occupies the next [cumulative, cumulative+dur].
        cumulative = 0.0
        color_results: list[dict] = []
        all_ok = True
        for i, c in enumerate(sorted_clips):
            dur = float(c["clip_end_offset"]) - float(c["clip_start_offset"])
            # Sample at the midpoint of the clip's slot in the rendered output.
            sample_ts = cumulative + dur / 2
            actual = sample_dominant_rgb(final_path, sample_ts)
            # Find expected color: midpoint of source = clip_start_offset + dur/2,
            # which lives in minute floor((start + dur/2) / 60).
            source_mid = float(c["clip_start_offset"]) + dur / 2
            minute_idx = int(source_mid // 60)
            minute_idx = max(0, min(minute_idx, len(COLOR_SCHEDULE) - 1))
            expected_label, expected_rgb = COLOR_SCHEDULE[minute_idx]
            dist = manhattan(actual, expected_rgb)
            ok = dist <= 60
            color_results.append(
                {
                    "clip_idx": i,
                    "source_mid_sec": source_mid,
                    "rendered_sample_sec": sample_ts,
                    "expected_label": expected_label,
                    "expected_rgb": expected_rgb,
                    "actual_rgb": actual,
                    "manhattan": dist,
                    "ok": ok,
                }
            )
            print(
                f"  clip {i}: src@{source_mid:.1f}s ({expected_label} "
                f"{expected_rgb}) rendered@{sample_ts:.1f}s actual={actual} "
                f"dist={dist} {'OK' if ok else 'FAIL'}"
            )
            all_ok = all_ok and ok
            cumulative += dur
        assert all_ok, f"color sampling failed: {color_results}"

        banner("Verify Phase D notification row exists")
        notifs = db_exec(
            "SELECT id::text, payload FROM coaching_sessions.notifications "
            "WHERE user_id = %s AND type = 'moment_reel_ready' "
            "AND payload->>'reel_id' = %s",
            (SEED_USER_ID, reel["id"]),
        )
        assert len(notifs) >= 1, (
            f"expected >=1 moment_reel_ready notification for reel {reel['id']}, "
            f"got {len(notifs)}"
        )
        print(f"  [ok] {len(notifs)} notification(s) found")

        banner("ALL CHECKS PASSED")
        print(f"  rendered mp4: {final_path}")
        print(f"  service log: {LOG_PATH}")
        return 0
    finally:
        banner("Teardown soccer-cam subprocess")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        print(f"  [ok] soccer-cam pid={proc.pid} stopped")


if __name__ == "__main__":
    sys.exit(main())
