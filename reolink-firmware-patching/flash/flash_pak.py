#!/usr/bin/env python3
"""Flash a .pak to the camera over HTTP, without the web UI.

Why this exists
---------------
Every flash in this tooling used to be a manual trip through
Settings -> Maintenance -> Local Upgrade. That is fine once; it is miserable
when you are iterating on a build. It also gives you no pre-flight check: the
web UI happily uploads a pak whose CRC is wrong and lets the camera reject it
several minutes later.

A naive scripted upload fails, which is probably why this wasn't automated
before: the camera's nginx sets ``client_max_body_size 512k`` in its ``http{}``
block, so POSTing a ~25 MB pak in one request returns **413 Request Entity Too
Large**. The web UI never hits that because it slices the file into ~38 KB
parts. This script reimplements that same chunked protocol.

The protocol (from the camera's own ``www/js/accountLogin.*.js``)
----------------------------------------------------------------
1. ``UpgradePrepare`` with ``{"restoreCfg": 0, "fileName": "<pak basename>"}``.
   ``restoreCfg: 0`` keeps your existing settings -- this is the scripted
   equivalent of *not* ticking "Reset Configuration".
2. For each ``bytesPerPiece = 38912`` slice, POST multipart/form-data to
   ``cgi-bin/api.cgi?cmd=Upgrade&file=upgrade-package`` with field name
   ``upgrade-package`` and a composite filename::

       <pak basename>&<uuid4>&<part index>&<total size>

   The uuid groups the parts of one upload; the camera reassembles into
   ``/mnt/tmp/img`` and tracks ``current recved data len:%d/%d``.
3. The camera verifies the Reolink CRC over the assembled pak before writing
   anything, so a truncated or corrupt upload is rejected rather than flashed.

Safety
------
* The pak's CRC is verified locally *before* a single byte is uploaded.
* The camera's own CRC check is the second gate.
* Only ``app`` / ``rootfs`` are ever touched by this tooling's builders, so the
  boot chain stays intact and a bad flash is recoverable through the same path.

Keep a stock pak around regardless -- see ``docs/PATCHING_GUIDE.md``.

Usage
-----
    python flash/flash_pak.py <path/to.pak>
    python flash/flash_pak.py <path/to.pak> --env ../camera.env
    python flash/flash_pak.py <path/to.pak> --no-wait

Reads ``CAMERA_IP`` / ``CAMERA_USER`` / ``CAMERA_PASS`` from ``camera.env``
next to this tooling's root (same file ``_camera_env.sh`` uses).
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import uuid

# The web UI's own slice size. Anything above nginx's 512k client_max_body_size
# is rejected with HTTP 413, so this must stay well under it.
BYTES_PER_PIECE = 38912

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_env(path: str) -> dict[str, str]:
    """Parse the shell-style camera.env that _camera_env.sh also sources."""
    env: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    for required in ("CAMERA_IP", "CAMERA_USER", "CAMERA_PASS"):
        if not env.get(required):
            sys.exit(f"ERROR: {required} missing from {path}")
    return env


def verify_crc_locally(pak: str) -> None:
    """Refuse to upload a pak the camera would reject anyway."""
    sys.path.insert(0, os.path.join(ROOT, "pak"))
    try:
        from reolink_crc import CRC_FIELD_OFFSET, compute  # noqa: PLC0415
    except ImportError:
        print("WARNING: pak/reolink_crc.py not importable; skipping local CRC check")
        return
    with open(pak, "rb") as fh:
        data = fh.read()
    stored = int.from_bytes(data[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 4], "little")
    computed = compute(data)
    if stored != computed:
        sys.exit(
            f"ERROR: CRC mismatch in {os.path.basename(pak)} "
            f"(stored 0x{stored:08x}, computed 0x{computed:08x}). "
            "The camera would reject this. Rebuild it."
        )
    print(f"local CRC ok: 0x{computed:08x}")


def api(conn: http.client.HTTPConnection, path: str, payload: list) -> dict:
    body = json.dumps(payload).encode()
    conn.request(
        "POST",
        path,
        body,
        {"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    raw = conn.getresponse().read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_raw": raw[:400].decode(errors="replace")}
    return parsed[0] if isinstance(parsed, list) and parsed else parsed


def login(conn: http.client.HTTPConnection, env: dict[str, str]) -> str:
    rsp = api(
        conn,
        "/cgi-bin/api.cgi?cmd=Login",
        [
            {
                "cmd": "Login",
                "action": 0,
                "param": {
                    "User": {
                        "userName": env["CAMERA_USER"],
                        "password": env["CAMERA_PASS"],
                    }
                },
            }
        ],
    )
    try:
        return rsp["value"]["Token"]["name"]
    except (KeyError, TypeError):
        sys.exit(f"ERROR: login failed: {rsp}")


def prepare(
    conn: http.client.HTTPConnection, token: str, name: str, attempts: int = 12
) -> None:
    """Open an upgrade session, waiting out a stale one if necessary.

    A previously aborted upload holds the session and answers "busy" until it
    times out, so retry rather than failing immediately.
    """
    for attempt in range(1, attempts + 1):
        rsp = api(
            conn,
            f"/cgi-bin/api.cgi?cmd=UpgradePrepare&token={token}",
            [
                {
                    "cmd": "UpgradePrepare",
                    "action": 0,
                    "param": {"restoreCfg": 0, "fileName": name},
                }
            ],
        )
        if rsp.get("code") == 0:
            print(f"UpgradePrepare ok ({rsp.get('value')})")
            return
        detail = rsp.get("error", {}).get("detail", rsp)
        print(f"  prepare {attempt}/{attempts}: {detail}; retrying in 15s")
        time.sleep(15)
    sys.exit(
        "ERROR: could not open an upgrade session (still busy). Reboot the camera and retry."
    )


def upload(
    conn: http.client.HTTPConnection, env: dict[str, str], token: str, pak: str
) -> None:
    name = os.path.basename(pak)
    total = os.path.getsize(pak)
    group = str(uuid.uuid4())
    parts = (total + BYTES_PER_PIECE - 1) // BYTES_PER_PIECE
    print(f"uploading {name}: {total} bytes in {parts} parts")

    started = time.time()
    text = ""
    with open(pak, "rb") as fh:
        for index in range(parts):
            piece = fh.read(BYTES_PER_PIECE)
            filename = f"{name}&{group}&{index}&{total}"
            boundary = uuid.uuid4().hex
            body = (
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="upgrade-package"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: application/octet-stream\r\n\r\n"
                ).encode()
                + piece
                + f"\r\n--{boundary}--\r\n".encode()
            )

            for _ in range(3):
                try:
                    conn.request(
                        "POST",
                        f"/cgi-bin/api.cgi?token={token}&cmd=Upgrade&file=upgrade-package",
                        body,
                        {
                            "Content-Type": f"multipart/form-data; boundary={boundary}",
                            "Content-Length": str(len(body)),
                        },
                    )
                    response = conn.getresponse()
                    text = response.read().decode(errors="replace")
                    break
                except OSError as exc:
                    print(f"\n  part {index}: {exc}; reconnecting")
                    conn.close()
                    conn = http.client.HTTPConnection(env["CAMERA_IP"], 80, timeout=60)
                    time.sleep(1)
            else:
                sys.exit(f"ERROR: part {index} failed after 3 attempts")

            if response.status != 200:
                sys.exit(
                    f"ERROR: part {index}/{parts} HTTP {response.status}: {text[:300]}"
                )

            if index % 50 == 0 or index == parts - 1:
                pct = 100.0 * (index + 1) / parts
                print(
                    f"  {index + 1}/{parts} ({pct:5.1f}%)  {time.time() - started:5.1f}s",
                    flush=True,
                )

    print(f"upload complete in {time.time() - started:.1f}s")
    print(f"camera response: {text.strip()[:200]}")


def wait_for_reboot(env: dict[str, str], timeout: int = 420) -> None:
    """The camera drops off while it flashes; report when it is back."""
    print("waiting for the camera to flash and reboot...")
    started = time.time()
    went_down = False
    while time.time() - started < timeout:
        alive = True
        try:
            conn = http.client.HTTPConnection(env["CAMERA_IP"], 80, timeout=5)
            conn.request("GET", "/")
            conn.getresponse().read()
            conn.close()
        except OSError:
            alive = False
        elapsed = int(time.time() - started)
        if not alive and not went_down:
            print(f"  [{elapsed:3d}s] camera went down (flashing)")
            went_down = True
        elif alive and went_down:
            print(f"  [{elapsed:3d}s] camera is back up")
            return
        time.sleep(3)
    print("WARNING: timed out waiting for the camera to come back; check it manually")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Flash a .pak to a Reolink camera over HTTP."
    )
    parser.add_argument("pak", help="path to the .pak to flash")
    parser.add_argument(
        "--env", default=os.path.join(ROOT, "camera.env"), help="camera.env path"
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="don't wait for the reboot to complete"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.pak):
        sys.exit(f"ERROR: no such file: {args.pak}")

    name = os.path.basename(args.pak)
    if not (name.startswith("IPC_") and name.endswith(".pak")):
        # The camera validates the filename and rejects anything that doesn't
        # look like a stock release name -- catch it before a long upload.
        print(
            f"WARNING: '{name}' does not look like a stock pak name; the camera may reject it"
        )

    env = load_env(args.env)
    verify_crc_locally(args.pak)

    conn = http.client.HTTPConnection(env["CAMERA_IP"], 80, timeout=60)
    token = login(conn, env)
    print(f"logged in to {env['CAMERA_IP']}")
    prepare(conn, token, name)
    upload(conn, env, token, args.pak)
    conn.close()

    if not args.no_wait:
        wait_for_reboot(env)


if __name__ == "__main__":
    main()
