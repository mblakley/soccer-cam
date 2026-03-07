"""Simple HTTP server for testing the dewarp viewer with tracking data.

Serves mobile/assets/ on port 8888 and proxies video from its original location.
Usage: python serve_viewer.py
Then open: http://localhost:8888/dewarp_viewer.html?src=/video/dahua_gameplay.mp4&track=/ball_track.json
"""

import http.server
import os

PORT = 8888
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "mobile", "assets")
VIDEO_PATH = os.path.normpath(
    os.path.join(
        os.path.expanduser("~"),
        "projects",
        "video-stitcher",
        "frontend",
        "public",
        "dahua_gameplay.mp4",
    )
)


class ViewerHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ASSETS_DIR, **kwargs)

    def do_GET(self):
        # Serve video from external path with Range support
        if self.path.startswith("/video/"):
            filename = self.path[7:]
            if filename == "dahua_gameplay.mp4" and os.path.exists(VIDEO_PATH):
                self._serve_video()
                return
            self.send_error(404)
            return
        super().do_GET()

    def _serve_video(self):
        file_size = os.path.getsize(VIDEO_PATH)
        range_header = self.headers.get("Range")

        if range_header:
            # Parse "bytes=start-end"
            range_spec = range_header.replace("bytes=", "")
            parts = range_spec.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(VIDEO_PATH, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(VIDEO_PATH, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def do_HEAD(self):
        if self.path.startswith("/video/"):
            filename = self.path[7:]
            if filename == "dahua_gameplay.mp4" and os.path.exists(VIDEO_PATH):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(VIDEO_PATH)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
        super().do_HEAD()


if __name__ == "__main__":
    print(f"Serving viewer from {ASSETS_DIR}")
    print(f"Video: {VIDEO_PATH}")
    print(
        f"Open: http://localhost:{PORT}/dewarp_viewer.html?src=/video/dahua_gameplay.mp4&track=/ball_track.json"
    )
    with http.server.HTTPServer(("", PORT), ViewerHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
