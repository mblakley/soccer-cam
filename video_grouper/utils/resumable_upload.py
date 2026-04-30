"""Resumable upload helper for TTT-minted Google resumable upload session URLs.

When TTT supplies an `upload.resumable_url` in a clip request payload, soccer-cam
PUTs the clip bytes directly to that URL. Google handles the auth (the URL carries
a short-lived token); the user's refresh token stays in TTT.

Supports 308 Resume Incomplete mid-stream for flaky connections and retries
transient 5xx.
"""

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024  # Google requires multiples of 256KB for chunks
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = [1, 2, 4, 8, 16]


class ResumableUploadError(RuntimeError):
    """Raised when a resumable upload fails after all retries."""


async def upload_to_resumable_url(
    file_path: str,
    resumable_url: str,
    mime_type: str,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> Optional[str]:
    """Upload a file to an existing resumable session URL.

    Returns the final destination URL (Drive webViewLink / YouTube URL) from
    the success response, or a generic marker if the response body has none.

    Raises ResumableUploadError on unrecoverable failure.
    """
    file_size = os.path.getsize(file_path)
    if file_size == 0:
        raise ResumableUploadError(f"Refusing to upload empty file {file_path}")

    logger.info(
        "Starting resumable upload of %s (%.1f MB) to %s",
        os.path.basename(file_path),
        file_size / (1024 * 1024),
        _redact(resumable_url),
    )

    offset = 0
    retry = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        with open(file_path, "rb") as fh:
            while offset < file_size:
                fh.seek(offset)
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1

                headers = {
                    "Content-Type": mime_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                }

                try:
                    resp = await client.put(
                        resumable_url, content=chunk, headers=headers
                    )
                except httpx.HTTPError as e:
                    retry = await _sleep_and_bump_retry(retry, e)
                    continue

                if resp.status_code in (200, 201):
                    return _final_url_from(resp)
                if resp.status_code == 308:
                    # Resume Incomplete — advance offset based on Range header
                    offset = _next_offset(resp, fallback=offset + len(chunk))
                    retry = 0
                    continue
                if 500 <= resp.status_code < 600:
                    retry = await _sleep_and_bump_retry(
                        retry, f"5xx {resp.status_code}: {resp.text[:200]}"
                    )
                    continue
                # 4xx or other — unrecoverable
                raise ResumableUploadError(
                    f"Upload failed with status {resp.status_code}: {resp.text[:300]}"
                )

    raise ResumableUploadError("Upload loop exited without a success response")


def _next_offset(resp: httpx.Response, *, fallback: int) -> int:
    """Parse 'Range: bytes=0-N' from a 308 response to find where to resume."""
    range_hdr = resp.headers.get("Range") or resp.headers.get("range")
    if not range_hdr or "-" not in range_hdr:
        return fallback
    try:
        _, last_byte = range_hdr.rsplit("-", 1)
        return int(last_byte) + 1
    except ValueError:
        return fallback


def _final_url_from(resp: httpx.Response) -> str:
    """Pull the user-facing URL from a Drive/YouTube upload response body."""
    try:
        body = resp.json()
    except Exception:
        return resp.text[:300] if resp.text else ""
    # Drive returns an id we can build a link from; webViewLink only if we requested it.
    for key in ("webViewLink", "id"):
        if key in body:
            return body[key] if key == "webViewLink" else _drive_view_link(body[key])
    return resp.text[:300] if resp.text else ""


def _drive_view_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


async def _sleep_and_bump_retry(retry: int, reason) -> int:
    """Sleep with exponential backoff and bump the retry counter; raise if exhausted."""
    if retry >= MAX_RETRIES:
        raise ResumableUploadError(
            f"Upload failed after {MAX_RETRIES} retries: {reason}"
        )
    delay = RETRY_BACKOFF_SECONDS[min(retry, len(RETRY_BACKOFF_SECONDS) - 1)]
    logger.warning(
        "Resumable upload retrying in %ds (attempt %d): %s", delay, retry + 1, reason
    )
    await asyncio.sleep(delay)
    return retry + 1


def _redact(url: str) -> str:
    """Strip query params from a URL for safe logging."""
    return url.split("?", 1)[0]
