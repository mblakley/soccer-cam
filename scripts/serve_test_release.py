"""Local stand-in for GitHub Releases API. Used for host-PC E2E
tests of the auto-upgrade flow without having to cut a real release.

Usage:

    uv run python scripts/serve_test_release.py \\
        --exe video_grouper/dist/VideoGrouperSetup.exe \\
        --version 0.3.7 \\
        --port 9876

Then in another shell:

    setx SOCCER_CAM_UPDATE_API_URL ^
        http://127.0.0.1:9876/repos/mblakley/soccer-cam/releases/latest
    Restart-Service VideoGrouperService

The service's UpdateCheckProcessor will hit this server next tick,
download the .exe, verify its digest (which we compute fresh at
startup), and spawn the installer.

The server mimics the *real* GitHub Releases API surface (Content-
Type, asset.digest, browser_download_url, streaming Content-Length
on the asset download) so the same code path exercised here is the
one that runs against production github.com. Tampering with the
served bytes via ``--tamper`` exercises the digest-mismatch refusal.

See the plan at
``~/.claude/plans/investigate-the-auto-upgrade-process-jiggly-gem.md``
for the broader test strategy.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("serve_test_release")


def _compute_digest(path: Path) -> tuple[str, int]:
    """Return (``sha256:hex``, size_bytes) for the .exe served."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return f"sha256:{h.hexdigest()}", size


def build_app(
    exe_path: Path,
    version: str,
    *,
    owner: str = "mblakley",
    repo: str = "soccer-cam",
    tamper: bool = False,
) -> FastAPI:
    """Construct the FastAPI app. Exposed for tests that want to
    drive the server in-process via FastAPI TestClient."""
    if not exe_path.exists():
        raise FileNotFoundError(f"Installer not found at {exe_path}")

    digest, size = _compute_digest(exe_path)
    logger.info("Serving v%s digest=%s size=%dB", version, digest, size)
    if tamper:
        logger.warning(
            "TAMPER MODE: served bytes will have one byte flipped. "
            "Digest verification SHOULD refuse to install."
        )

    app = FastAPI(title="serve_test_release", version=version)

    @app.get(f"/repos/{owner}/{repo}/releases/latest")
    def releases_latest(request: Request) -> JSONResponse:
        # Bare-minimum subset of the real GitHub Releases response
        # shape -- the fields our updater actually reads.
        return JSONResponse(
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Media-Type": "github.v3; format=json",
            },
            content={
                "tag_name": f"v{version}",
                "name": f"VideoGrouper {version}",
                "published_at": datetime.now(UTC).isoformat(),
                "body": f"Local test release v{version}. Served by serve_test_release.py.",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": "VideoGrouperSetup.exe",
                        "browser_download_url": (
                            f"http://{request.url.hostname}:{request.url.port}"
                            "/assets/VideoGrouperSetup.exe"
                        ),
                        "size": size,
                        "digest": digest,
                        "content_type": "application/octet-stream",
                    }
                ],
            },
        )

    @app.get("/assets/VideoGrouperSetup.exe")
    def asset_download() -> StreamingResponse:
        def stream():
            with exe_path.open("rb") as f:
                first = True
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    if tamper and first:
                        chunk = bytes([chunk[0] ^ 0x01]) + chunk[1:]
                        first = False
                    yield chunk

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={"Content-Length": str(size)},
        )

    # /healthz lets E2E tests poll-wait for the server to be ready
    # without race conditions.
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Catch-all 404 helps debug bad SOCCER_CAM_UPDATE_API_URL values.
    @app.get("/{path:path}")
    def not_found(path: str):
        raise HTTPException(
            status_code=404,
            detail=(
                f"No route for /{path}. Expected /repos/{owner}/{repo}/releases/latest"
            ),
        )

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--exe", required=True, type=Path, help="Path to VideoGrouperSetup.exe"
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Version string for the fake release (e.g. 0.3.7)",
    )
    parser.add_argument("--port", type=int, default=9876)
    parser.add_argument("--owner", default="mblakley")
    parser.add_argument("--repo", default="soccer-cam")
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="Serve a bit-flipped copy of the .exe to exercise the digest-mismatch refusal",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    app = build_app(
        args.exe.resolve(),
        args.version,
        owner=args.owner,
        repo=args.repo,
        tamper=args.tamper,
    )
    logger.info(
        "Point the service at: http://127.0.0.1:%d/repos/%s/%s/releases/latest",
        args.port,
        args.owner,
        args.repo,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
