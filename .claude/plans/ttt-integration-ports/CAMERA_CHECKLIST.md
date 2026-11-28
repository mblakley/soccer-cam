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

### 2026-11-28 — Reolink Duo 3 PoE, run from `main` at `a0801f2`

Run by the firmware session with Mark's go-ahead. The camera was at
192.168.86.22, not the .200 in `config.ini`, so the address was overridden in
memory. It runs patched firmware (build 4923 on stock v3.0.0.4867); none of the
patches target account handling. Serial and MAC values are left out
of this file on purpose.

**1. Self-registration inputs**

| check | result |
|---|---|
| `serial_number` non-empty | **Yes**, 14 characters. Not compared with the label (nobody read the label). |
| `mac_address` non-empty | **No. `get_device_info()` returns an empty MAC on this camera.** `GetDevInfo` has no `mac` field here (its keys: `B485, IOInputNum, IOOutputNum, audioNum, buildDay, cfgVer, channelNum, detail, diskNum, exactType, firmVer, frameworkVer, hardVer, itemNo, model, name, pakSuffix, serial, type, wifi`). The MAC is in `GetLocalLink` (`value.LocalLink.mac`), and that one matches the camera's entry in the PC's ARP table. |
| `model` identifies the camera | Yes: `Reolink Duo 3 PoE`, type `IPC`. |

Consequence: `device_link_processor` takes the MAC from `get_device_info()`
(`device_link_processor.py:230`), so for this camera it registers with no MAC.
`ReolinkCamera.get_device_info` needs to read `GetLocalLink` for it.

**2a / 2b. What the camera supports**

- `GetUser` lists accounts with their level. Before the test: one account,
  `admin` (level `admin`). Nothing existing was changed.
- Levels offered: `guest`, `admin`. User name 1–31 characters, password 6–31.
- **No maximum account count is reported**, by `GetUser`'s range or by
  `GetAbility`. Unknown.
- Not an admin-only model: a second account was created.

**2c. A non-admin login** (`sc_probe`, level `guest`, random password, created
and deleted in one run)

| check | result |
|---|---|
| `AddUser` with admin credentials | Succeeded |
| Guest login | Works |
| Guest can list recordings | **Yes**: 17 files for 2026-08-22, the same 17 the admin sees |
| Guest can download a recording | **Yes**: the smallest file, 1,781,896 bytes, arrived complete through `download_file` (`download_protocol = auto`; which transport it took was not recorded) |
| Guest can read device info | Yes: model and the same serial as admin. MAC through `GetLocalLink` also works for the guest |
| What the guest is refused | `SetEnc` with the camera's own current values failed (`rspCode -13`, "set config failed"; the same code an invalid value gets, so the code alone does not say "permission"). `GetAbility` as the guest reports read-only (4) for `enc`, `isp`, `image`, `recCfg`, `recDownload`, `recReplay`, `snap`, `mask`, `sdCard`, where admin has read-write (6); and 0 for `upgrade`, `reboot`, `restore`, `exportCfg`, `importCfg`. Only that one write was attempted |
| Also allowed, worth knowing | The guest can call `GetUser` and see every account name and level. `user` ability is 6 for the guest (not exercised; presumably its own password) |
| `DelUser`, original list back | Yes: `GetUser` shows only `admin` again, confirmed a second time from a separate login |

**So:** a per-manager `guest` account can do soccer-cam's read side on this
camera — list, download, device info — and was refused the one settings write tried.
It cannot do what needs a write: `apply_optimal_settings`, starting or stopping
recording, deleting files (none of those were attempted as the guest; the
ability table says read-only). The decision in section 3 is Mark's.

Not covered: the account limit; the firmware-upgrade path as a guest; any
Dahua camera.

Outside this checklist but relevant to "revocable" accounts: on the patched
firmware this camera also serves `/downloadfile/` with no login and has a root
shell on tcp/2323, so a camera account is not the only way in on the LAN.
