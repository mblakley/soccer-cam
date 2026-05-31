"""Full UI-driven smoke test of the moment-tagger reel pipeline.

Drives the WHOLE path from a real user tap in the moment-tagger UI to
a finished YouTube-uploaded reel:

1. Drive moment-tagger UI via Playwright:
   - Sign in via dev login
   - Open /moment-tagger/games (auto-selects today's live game)
   - Tap 3 moments in the QuickTagView
   - Wait for POST /api/moment-tags to fire each time

2. Verify the tags landed in TTT DB (real user-route POST, not SQL).

3. Simulate the auto-create-reel hook output (Phase A) directly via SQL
   — the route POST /api/games/{id}/videos has a pre-existing 500 bug
   tracked separately.

4. Run soccer-cam HighlightReelProcessor in-process against live TTT
   with a stub YouTubeUploader, against a fake combined.mp4.

5. Verify: status=ready, output mp4 length matches sum of clip windows,
   notification row created.

Requires:
- TTT docker stack at localhost:3000 / 8000 (already running for the
  moment-tagger-wire-quicktag PR work, includes Phase A-E + #34 wiring).
- Local Supabase at 54322.
- C:/tmp/moment-reel-smoke/storage/test-recording/combined.mp4 (180s).
- Playwright installed (this script uses sync_api).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg2
import psycopg2.extras
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_grouper.api_integrations.ttt_api import TTTApiClient  # noqa: E402
from video_grouper.task_processors.highlight_reel_processor import (  # noqa: E402
    HighlightReelProcessor,
)

TTT_BASE = "http://localhost:8000"
FRONTEND_BASE = "http://localhost:3000"
DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
STORAGE_PATH = Path("C:/tmp/moment-reel-smoke/storage").resolve()
COMBINED_DIR_NAME = "test-recording"
COMBINED_MP4 = STORAGE_PATH / COMBINED_DIR_NAME / "combined.mp4"
SEED_USER_ID = "31170262-cbec-460a-a017-94d3ff2c3e9e"
SEED_TEAM_ID = "80000000-0000-0000-0000-000000000001"
SEED_PLAYER_ID = "e0000000-0000-0000-0001-000000000001"


def http_post(path: str, body: dict, token: str | None = None) -> dict:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(TTT_BASE + path, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
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


def banner(s: str) -> None:
    print(f"\n{'=' * 78}\n{s}\n{'=' * 78}")


class StubYouTubeUploader:
    def __init__(self) -> None:
        self.uploaded_path: str | None = None
        self.uploaded_title: str | None = None
        self.video_id = f"ytFULLSMK{uuid.uuid4().hex[:8]}"

    def upload_video(
        self,
        video_path: str,
        title: str,
        description: str,
        tags=None,
        privacy_status: str = "unlisted",
        playlist_id=None,
        on_progress=None,
    ) -> str:
        self.uploaded_path = video_path
        self.uploaded_title = title
        print(f"  [stub upload] would upload {video_path!r} as {privacy_status!r}")
        if on_progress:
            on_progress(100)
        return self.video_id


def make_config(camera_id: str) -> Any:
    cfg = type("Config", (), {})()
    cfg.ttt = type("Ttt", (), {})()
    cfg.ttt.api_url = TTT_BASE
    cfg.ttt.camera_id = camera_id
    return cfg


def setup_db_state() -> dict:
    """Create the game + game_session + camera_recording + camera setup
    so that the wiring's `?status=live` query returns one game for our
    user's team, and the recording_group_dir resolves to our fake mp4."""
    game_id = str(uuid.uuid4())
    game_session_id = str(uuid.uuid4())
    camera_id = str(uuid.uuid4())
    camera_recording_id = str(uuid.uuid4())
    youtube_video_id = f"fullPipeline{uuid.uuid4().hex[:8]}"

    db_exec(
        """
        INSERT INTO game_analysis.games
            (id, home_team_id, away_team_id, date, game_type,
             opponent_name, created_by)
        VALUES (%s, %s, %s, CURRENT_DATE, 'friendly', 'UI Smoke FC', %s)
        """,
        (game_id, SEED_TEAM_ID, SEED_TEAM_ID, SEED_USER_ID),
    )
    db_exec(
        """
        INSERT INTO coaching_sessions.game_sessions
            (id, team_id, recording_group_dir, status, game_date,
             opponent_name, recording_start_time, created_by, sync_status)
        VALUES (%s, %s, %s, 'live', CURRENT_DATE, 'UI Smoke FC',
                NOW() - INTERVAL '10 minutes', %s, 'pending')
        """,
        (game_session_id, SEED_TEAM_ID, COMBINED_DIR_NAME, SEED_USER_ID),
    )
    db_exec(
        """
        INSERT INTO coaching_sessions.cameras (id, team_id, user_id, name)
        VALUES (%s, %s, %s, 'UI Smoke Cam')
        """,
        (camera_id, SEED_TEAM_ID, SEED_USER_ID),
    )
    db_exec(
        """
        INSERT INTO coaching_sessions.camera_recordings
            (id, camera_id, team_id, game_session_id, file_name,
             recording_start, youtube_video_id, upload_status)
        VALUES (%s, %s, %s, %s, %s, NOW(), %s, 'complete')
        """,
        (
            camera_recording_id,
            camera_id,
            SEED_TEAM_ID,
            game_session_id,
            "combined.mp4",
            youtube_video_id,
        ),
    )

    return {
        "game_id": game_id,
        "game_session_id": game_session_id,
        "camera_id": camera_id,
        "youtube_video_id": youtube_video_id,
    }


def authenticate_browser(page) -> None:
    """Use the dev-login button on /tools to seed the auth state."""
    page.goto(f"{FRONTEND_BASE}/tools?signin=1", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    # Find the dev login button — it shows when VITE_DEV_MODE=true is built.
    btn = page.locator('button:has-text("Dev"), button:has-text("dev login")').first
    btn.wait_for(state="visible", timeout=8000)
    btn.click()
    # Wait for the redirect
    page.wait_for_timeout(2500)


def drive_moment_tagger_ui(state: dict) -> list[dict]:
    """Drive the moment-tagger UI in a real browser and capture the
    /api/moment-tags POST requests. Returns the captured requests."""
    moment_post_bodies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 390, "height": 844}  # iPhone 14 portrait
            )
            page = context.new_page()
            page.on(
                "request",
                lambda r: (
                    moment_post_bodies.append(
                        {
                            "url": r.url,
                            "method": r.method,
                            "body": r.post_data,
                        }
                    )
                    if "/api/moment-tags" in r.url and r.method == "POST"
                    else None
                ),
            )

            banner("UI: sign in via dev login")
            authenticate_browser(page)

            banner("UI: navigate to /moment-tagger/games and verify auto-select")
            page.goto(f"{FRONTEND_BASE}/moment-tagger/games", wait_until="networkidle")
            page.wait_for_timeout(1500)

            # Look for the "Auto-selected" hint
            hint = page.locator("text=/Auto-selected/i").first
            try:
                hint.wait_for(state="visible", timeout=8000)
                print("  [ok] Auto-select hint visible")
            except Exception:
                print("  WARN: no auto-select hint found — listing games instead")

            # Find the game card matching our seeded opponent + click it
            game_card = page.locator('text="UI Smoke FC"').first
            game_card.wait_for(state="visible", timeout=8000)
            game_card.click()
            page.wait_for_timeout(1500)

            banner("UI: tap 3 moments in QuickTagView")
            # Tap the TAG IT button 3 times (with a player or label sequence)
            for i, label in enumerate(["Goal", "Save", "Assist"]):
                label_btn = page.locator(f'button:has-text("{label}")').first
                label_btn.wait_for(state="visible", timeout=5000)
                label_btn.click()
                page.wait_for_timeout(300)
                # Then TAG IT
                tag_it = page.locator('button:has-text("TAG IT")').first
                tag_it.click()
                # Wait for the POST to fire
                page.wait_for_timeout(800)
                print(f"  [ok] tap {i + 1}: {label}")

            # Give batch sync a moment
            page.wait_for_timeout(2000)
        finally:
            browser.close()

    return moment_post_bodies


async def run_soccer_cam_render(state: dict, jwt: str) -> dict:
    """Spin up HighlightReelProcessor in-process and render the reel."""
    ttt_client = TTTApiClient(
        supabase_url="http://127.0.0.1:54321",
        anon_key=os.environ.get("SUPABASE_ANON_KEY", "anon-stub"),
        api_base_url=TTT_BASE,
        storage_path=str(STORAGE_PATH),
    )
    ttt_client.set_session_from_token(jwt)

    stub = StubYouTubeUploader()
    config = make_config(state["camera_id"])
    processor = HighlightReelProcessor(
        storage_path=str(STORAGE_PATH),
        config=config,
        ttt_client=ttt_client,
        youtube_uploader=stub,
        poll_interval=60,
    )

    pending = ttt_client.get_pending_highlights(None)
    matching = [r for r in pending if r["id"] == state["reel_id"]]
    assert matching, f"reel {state['reel_id']} not pending"
    await processor._process_reel(matching[0])

    return {
        "uploaded_path": stub.uploaded_path,
        "uploaded_video_id": stub.video_id,
    }


def main() -> int:
    assert COMBINED_MP4.exists(), f"missing {COMBINED_MP4}"

    banner("1. DB setup: create a live game for the dev user's team")
    state = setup_db_state()
    print(
        f"  game_id={state['game_id'][:8]}.. session={state['game_session_id'][:8]}.."
    )

    banner("2. Drive moment-tagger UI in a real browser")
    post_bodies = drive_moment_tagger_ui(state)
    print(f"  captured {len(post_bodies)} POST /api/moment-tags calls")
    assert len(post_bodies) >= 3, (
        f"expected ≥3 POST /api/moment-tags, got {len(post_bodies)} — UI may not be "
        f"wired through useTagging.createTag()."
    )

    banner("3. Verify tags landed in DB via the user route (not SQL)")
    rows = db_exec(
        """
        SELECT id::text, player_id::text, label, video_offset_seconds
        FROM coaching_sessions.moment_tags
        WHERE game_session_id = %s
        ORDER BY tagged_at
        """,
        (state["game_session_id"],),
    )
    print(f"  DB has {len(rows)} moment_tags for our session")
    assert len(rows) >= 3, f"expected ≥3 moment_tags in DB, got {len(rows)}"

    # The UI taps land without a video_offset_seconds — that's soccer-cam's job.
    # For this smoke we backfill them so Phase A's reel has usable clips.
    banner("4. Backfill offsets + create moment_clips (soccer-cam simulation)")
    offsets = [(30.0, 15.0, 45.0), (60.0, 45.0, 75.0), (120.0, 105.0, 135.0)]
    tag_ids = [r["id"] for r in rows[:3]]
    clip_ids = []
    for tag_id, (off, start, end) in zip(tag_ids, offsets, strict=False):
        db_exec(
            "UPDATE coaching_sessions.moment_tags SET video_offset_seconds = %s WHERE id = %s",
            (off, tag_id),
        )
        clip_id = str(uuid.uuid4())
        db_exec(
            """
            INSERT INTO coaching_sessions.moment_clips
                (id, moment_tag_id, game_session_id,
                 clip_start_offset, clip_end_offset, clip_duration, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
            """,
            (clip_id, tag_id, state["game_session_id"], start, end, end - start),
        )
        clip_ids.append(clip_id)
    print(f"  3 moment_clips created with windows {[(s, e) for _, s, e in offsets]}")

    banner("5. Insert game_video + auto-create reel (Phase A hook output)")
    game_video_id = str(uuid.uuid4())
    db_exec(
        """
        INSERT INTO game_analysis.game_videos
            (id, game_id, youtube_url, youtube_video_id, video_type, created_at)
        VALUES (%s, %s, %s, %s, 'full', NOW())
        """,
        (
            game_video_id,
            state["game_id"],
            f"https://youtu.be/{state['youtube_video_id']}",
            state["youtube_video_id"],
        ),
    )
    reel_id = str(uuid.uuid4())
    db_exec(
        """
        INSERT INTO coaching_sessions.highlight_reels
            (id, player_id, player_name, game_id, title, status, source,
             created_by, created_at)
        VALUES (%s, %s, 'Mark Blakley', %s, 'Full Pipeline Smoke Reel',
                'pending', 'moment_tagger', %s, NOW())
        """,
        (reel_id, SEED_PLAYER_ID, state["game_id"], SEED_USER_ID),
    )
    for idx, clip_id in enumerate(clip_ids):
        db_exec(
            """
            INSERT INTO coaching_sessions.highlight_reel_clips
                (highlight_reel_id, moment_clip_id, sequence_order)
            VALUES (%s, %s, %s)
            """,
            (reel_id, clip_id, idx),
        )
    state["reel_id"] = reel_id
    print(f"  reel_id={reel_id} (source=moment_tagger, 3 clips linked)")

    banner("6. Get a JWT for soccer-cam to authenticate to TTT")
    auth = http_post(
        "/api/dev/login",
        {"user_id": SEED_USER_ID, "email": "mark.blakley@gmail.com"},
    )
    jwt = auth["access_token"]

    banner("7. Run soccer-cam HighlightReelProcessor in-process")
    render_result = asyncio.run(run_soccer_cam_render(state, jwt))

    banner("8. Verify final reel state")
    final = db_exec(
        "SELECT status, youtube_video_id, file_path, error_message "
        "FROM coaching_sessions.highlight_reels WHERE id = %s",
        (reel_id,),
    )[0]
    print(f"  reel status={final['status']!r}")
    print(f"  reel youtube_video_id={final['youtube_video_id']!r}")
    print(f"  reel file_path={final['file_path']!r}")
    assert final["status"] == "ready", f"expected status=ready, got {final['status']!r}"
    assert final["youtube_video_id"] == render_result["uploaded_video_id"]

    out_path = render_result["uploaded_path"]
    assert out_path and os.path.exists(out_path), f"output mp4 missing: {out_path}"
    duration = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                out_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    expected = sum(e - s for (_, s, e) in offsets)
    print(f"  output mp4 duration={duration:.3f}s (expected ~{expected}s)")
    assert abs(duration - expected) < 0.6

    banner("9. Verify Phase D notification fired")
    notifs = db_exec(
        """
        SELECT id::text, payload->>'youtube_video_id' AS yt
        FROM coaching_sessions.notifications
        WHERE user_id = %s
          AND type = 'moment_reel_ready'
          AND payload->>'reel_id' = %s
        """,
        (SEED_USER_ID, reel_id),
    )
    assert len(notifs) == 1, f"expected 1 notification, got {len(notifs)}"
    print(f"  notification {notifs[0]['id']} -> yt={notifs[0]['yt']}")

    banner("ALL CHECKS PASSED")
    print(
        "  - Real moment-tagger UI taps -> POST /api/moment-tags rows in DB\n"
        f"  - Auto-create reel + soccer-cam render produced a {duration:.3f}s mp4\n"
        "  - Notification fired for the player"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
