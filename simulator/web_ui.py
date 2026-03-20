"""Shared web dashboard for camera simulators.

Serves a single-page dashboard on port 8080 with:
- Status panel (camera type, uptime, ports, credentials, connections)
- Recording schedule state
- Recordings table with delete buttons
- Upload form (file + time range + channel)
- Test pattern generation
- Activity log (auto-refreshing)
"""

import logging
import os
import tempfile
import time
from datetime import datetime

from aiohttp import web

logger = logging.getLogger(__name__)


def setup_web_ui(
    app: web.Application,
    storage,
    camera_type: str,
    username: str,
    password: str,
    baichuan_server=None,
):
    """Register web UI routes on the given aiohttp app."""
    start_time = time.time()
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")

    async def handle_index(request):
        with open(os.path.join(templates_dir, "index.html")) as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def handle_api_status(request):
        uptime = int(time.time() - start_time)
        bc_connections = baichuan_server.active_connections if baichuan_server else 0
        activity_log = app.get("activity_log", [])

        from reolink.http_api import _schedule_state

        schedule = {}
        if camera_type == "reolink":
            schedule = {
                "TIMING": "1" in _schedule_state.get("TIMING", ""),
                "MD": "1" in _schedule_state.get("MD", ""),
            }

        return web.json_response(
            {
                "camera_type": camera_type,
                "uptime_seconds": uptime,
                "username": username,
                "ports": {
                    "http": 80,
                    "web_ui": 8080,
                    "baichuan": 9000 if camera_type == "reolink" else None,
                },
                "baichuan_connections": bc_connections,
                "recording_count": len(storage.recordings),
                "schedule": schedule,
                "activity_log": list(activity_log)[-50:],
            }
        )

    async def handle_api_recordings(request):
        recordings = []
        for rec in storage.recordings:
            recordings.append(
                {
                    "id": rec["id"],
                    "filename": rec["filename"],
                    "camera_path": rec["camera_path"],
                    "start_time": rec["start_time"],
                    "end_time": rec["end_time"],
                    "size": rec["size"],
                    "channel": rec["channel"],
                }
            )
        return web.json_response(recordings)

    async def handle_api_upload(request):
        reader = await request.multipart()

        file_data = None
        start_time_str = ""
        end_time_str = ""
        channel = 0

        while True:
            part = await reader.next()
            if part is None:
                break

            if part.name == "file":
                file_data = await part.read()
                filename = part.filename or "upload.mp4"
            elif part.name == "start_time":
                start_time_str = (await part.read()).decode()
            elif part.name == "end_time":
                end_time_str = (await part.read()).decode()
            elif part.name == "channel":
                channel = int((await part.read()).decode())

        if not file_data:
            return web.json_response({"error": "No file uploaded"}, status=400)

        try:
            st = datetime.fromisoformat(start_time_str)
            et = datetime.fromisoformat(end_time_str)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid time format"}, status=400)

        # Save to temp file, then add to storage
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=os.path.splitext(filename)[1]
        )
        tmp.write(file_data)
        tmp.close()

        try:
            rec = storage.add_recording(tmp.name, st, et, channel)
            return web.json_response(
                {"id": rec["id"], "camera_path": rec["camera_path"]}
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    async def handle_api_generate(request):
        data = await request.json()
        start_time_str = data.get("start_time", "")
        duration = int(data.get("duration", 60))
        channel = int(data.get("channel", 0))

        try:
            st = datetime.fromisoformat(start_time_str)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid time format"}, status=400)

        rec = storage.generate_test_recording(st, duration, channel)
        if rec:
            return web.json_response(
                {"id": rec["id"], "camera_path": rec["camera_path"]}
            )
        return web.json_response(
            {"error": "Failed to generate test recording"}, status=500
        )

    async def handle_api_delete(request):
        rec_id = request.match_info["id"]
        if storage.delete_recording(rec_id):
            return web.json_response({"deleted": True})
        return web.json_response({"error": "Not found"}, status=404)

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/recordings", handle_api_recordings)
    app.router.add_post("/api/upload", handle_api_upload)
    app.router.add_post("/api/generate", handle_api_generate)
    app.router.add_delete("/api/recordings/{id}", handle_api_delete)
