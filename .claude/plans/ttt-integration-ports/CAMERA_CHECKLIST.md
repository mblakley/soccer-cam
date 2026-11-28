# Camera checklist — run when the camera is next on the network

Written 2026-11-27. Camera: `[CAMERA.reolink]` in `shared_data/config.ini`
(Reolink, 192.168.86.200, user `admin`). Run from the soccer-cam repo root on the
same network. Record results (dated) at the bottom, then in `DECISIONS.md` if
they change a decision.

Every step is read-only except **2c**, which creates one throwaway account and
deletes it in the same step.

## Shared setup (paste before each step)

```python
import asyncio, json
from pathlib import Path
from video_grouper.utils.config import load_config
from video_grouper.cameras.reolink import ReolinkCamera

cfg = load_config(Path("shared_data/config.ini"))
cam = ReolinkCamera(cfg.cameras[0], "shared_data")

async def api(cmd, param=None, camera=cam):
    client, close = camera._get_client()
    try:
        return await camera._api_call(client, cmd, param or {}, log_name=cmd.lower())
    finally:
        if close:
            await client.aclose()
```

Run each step as `uv run python - <<'EOF' … EOF` with the setup above it.

## 1. Self-registration inputs (TTT device registration)

Registration is keyed on the serial, and joining an already-registered camera
also needs the MAC (decided 2026-11-27).

```python
info = asyncio.run(cam.get_device_info())
print({k: info.get(k) for k in ("serial_number", "mac_address", "model", "device_type", "firmware_version")})
```

- [ ] `serial_number` is non-empty and matches the label.
- [ ] `mac_address` is non-empty and is the camera's own MAC (compare the router's client list).
- [ ] `model` identifies the camera (Duo 3 PoE?).

## 2. Multiple accounts on the camera

Can each camera manager have their own camera login (revocable), and what can a
non-admin login do?

**2a. What the camera supports**

```python
ab = asyncio.run(api("GetAbility", {"User": {"userName": cam.username}}))
print(json.dumps(ab, indent=1)[:4000])
users = asyncio.run(api("GetUser"))
print(json.dumps(users, indent=1))
```

- [ ] `GetUser` lists the accounts, with each one's level (`admin` / `guest`).
- [ ] `GetUser`'s `range` / `initial` (or `GetAbility`) shows the maximum number of accounts.
- [ ] This model is not "admin only". Reolink's battery-powered models are.

**2b. Note the existing accounts.** Don't change or remove any of them.

**2c. A non-admin login (creates `sc_probe`, then deletes it)**

```python
PW = "Probe-" + __import__("secrets").token_hex(6)
print(asyncio.run(api("AddUser", {"User": {"userName": "sc_probe", "password": PW, "level": "guest"}})))
try:
    import copy
    probe_cfg = copy.deepcopy(cfg.cameras[0]); probe_cfg.username = "sc_probe"; probe_cfg.password = PW
    probe = ReolinkCamera(probe_cfg, "shared_data")
    from datetime import datetime, timedelta
    files = asyncio.run(probe.get_file_list(start_time=datetime.now() - timedelta(days=7), end_time=datetime.now()))
    print("guest file list:", len(files or []))
    print("guest device info:", asyncio.run(probe.get_device_info()))
    # download: try one small file to a temp path and report success / HTTP status
finally:
    print(asyncio.run(api("DelUser", {"User": {"userName": "sc_probe"}})))
    print(json.dumps(asyncio.run(api("GetUser")), indent=1))  # sc_probe gone?
```

- [ ] `AddUser` succeeds with admin credentials.
- [ ] The guest login works.
- [ ] The guest can **list** recordings.
- [ ] The guest can **download** a recording. This decides whether a per-manager non-admin account can do soccer-cam's job.
- [ ] The guest can read device info (the serial and MAC used for registration).
- [ ] What the guest is refused (settings and config changes) matches a "camera manager" role.
- [ ] `DelUser` removes it, and `GetUser` shows the original list.

## 3. Afterwards

- Record the results below, dated.
- If 2c passes, there's a decision for Mark: give each camera manager their own camera account at setup (created with the admin login by whoever has the camera; revocable when the camera changes hands).
- Dahua (if one is set up): `userManager.cgi?action=getUserInfoAll` for the
  account list. Per the forums, accounts can only join groups that already exist.

## Results

_(not run yet)_
