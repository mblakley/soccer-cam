#!/usr/bin/env python3
"""Windows-native Reolink Duo 3 PoE firmware patcher — no WSL required.

Builds a patched .pak entirely on Windows:
  * pak parse / repack / Reolink CRC      -> pure Python (pak/)
  * device HTTP /downloadfile/ unlock      -> size-preserving byte replace
  * router bitrate cap                      -> 4-byte instruction patch
  * record-at-home gate in start_app        -> baked home gateway MAC(s)
  * squashfs unpack / repack                -> mksquashfs/unsquashfs from
                                              `squashfs-tools` (install once:
                                              `scoop install squashfs-tools`)
  * home gateway MAC auto-detect            -> PowerShell (host is on the home LAN)

This is the engine the config-UI "Patch camera firmware" button will call.
Reolink-only. UNVERIFIED on a camera — bench-test per docs/RECORD_GATE_DESIGN.md
before field use.

Usage:
  python patch_firmware.py <stock.pak> <out.pak> [--kbps 20480]
                           [--home-mac aa:bb:cc:dd:ee:ff ...] [--no-gate]
                           [--keep-work]

If --home-mac is omitted (and the gate is enabled) the host's default-gateway
MAC is auto-detected and baked in.
"""

from __future__ import annotations

import argparse
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAK_DIR = HERE.parent / "pak"
if PAK_DIR.is_dir():  # source layout; in a frozen build pak/ is bundled as modules
    sys.path.insert(0, str(PAK_DIR))

from pak_repack import parse_section_table, repack  # noqa: E402
from reolink_crc import compute as reolink_crc, CRC_FIELD_OFFSET  # noqa: E402


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> Path:
    """Root of bundled data/binaries: PyInstaller's _MEIPASS when frozen,
    else the repo's reolink-firmware-patching/ dir."""
    return Path(getattr(sys, "_MEIPASS", str(HERE.parent)))


def gate_template_path() -> Path:
    if _frozen():
        return _bundle_dir() / "runtime" / "recordgate" / "start_app_gate.template"
    return HERE.parent / "runtime" / "recordgate" / "start_app_gate.template"


# --- byte-patch constants (identical to builds/build_recordgate.sh) ---
HTTP_SRC = (
    b"location /downloadfile/ {\n"
    b"            internal;\n"
    b"            limit_conn one 1;\n"
    b"            limit_rate 1024k;\n"
    b"            alias /mnt/sda/;\n"
    b"        }"
)
HTTP_DST = (
    b"location /downloadfile/ {\n"
    b"           #internal;\n"
    b"            limit_conn one 1;\n"
    b"            limit_rate 0;    \n"
    b"            alias /mnt/sda/;\n"
    b"        }"
)
BITRATE_OFFSET = 0x6351C
BITRATE_SRC = bytes.fromhex("0b008652")  # mov w11, #0x3000

MKSQUASHFS_ARGS = [
    "-comp",
    "xz",
    "-b",
    "262144",
    "-noappend",
    "-no-progress",
    "-no-exports",
    "-all-root",
    "-mkfs-time",
    "0",
    "-all-time",
    "0",
]


def die(msg: str) -> None:
    sys.exit(f"ERROR: {msg}")


def find_tool(name: str) -> str:
    # In a frozen build the squashfs binaries are bundled next to the exe payload.
    if _frozen():
        for cand in (
            _bundle_dir() / f"{name}.exe",
            _bundle_dir() / "bin" / f"{name}.exe",
        ):
            if cand.exists():
                return str(cand)
    p = shutil.which(name) or shutil.which(name + ".exe")
    if not p:
        die(
            f"'{name}' not found. Install the Windows squashfs tools once:\n"
            f"    scoop install squashfs-tools\n"
            f"(7-Zip can read squashfs but cannot create it, so it is not enough.)"
        )
    return p


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def detect_home_mac() -> str | None:
    """Host default-gateway MAC (lowercased colon form). The build host is on
    the home LAN, so its gateway MAC is the camera's home gateway MAC."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$r=Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric,ifMetric | Select-Object -First 1;"
        "$n=Get-NetNeighbor -IPAddress $r.NextHop | Where-Object {$_.LinkLayerAddress} | Select-Object -First 1;"
        "$n.LinkLayerAddress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:
        return None
    m = re.search(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", out or "")
    return m.group(0).replace("-", ":").lower() if m else None


def section_blob(stock: bytes, name: str) -> bytes:
    for s in parse_section_table(stock):
        if s["name"] == name and s["size"] > 0:
            return stock[s["off"] : s["off"] + s["size"]]
    die(f"section '{name}' not found in pak")


def patch_device(app_dir: Path) -> None:
    p = app_dir / "device"
    data = p.read_bytes()
    if data.count(HTTP_DST) >= 1 and HTTP_SRC not in data:
        print("   device: /downloadfile/ already unlocked")
        return
    n = data.count(HTTP_SRC)
    if n != 2:
        die(f"device: expected 2 /downloadfile/ blocks, found {n}")
    out = data.replace(HTTP_SRC, HTTP_DST)
    if len(out) != len(data):
        die("device: HTTP unlock changed file size (must be byte-preserving)")
    p.write_bytes(out)
    print("   device: /downloadfile/ unlocked")


def patch_router(app_dir: Path, kbps: int) -> None:
    p = app_dir / "router"
    data = bytearray(p.read_bytes())
    inst = 0x5280000B | (kbps << 5)
    dst = struct.pack("<I", inst)
    actual = bytes(data[BITRATE_OFFSET : BITRATE_OFFSET + 4])
    if actual == dst:
        print(f"   router: bitrate already {kbps} kbps")
        return
    if actual != BITRATE_SRC:
        die(
            f"router[{hex(BITRATE_OFFSET)}] mismatch: got {actual.hex()} "
            f"(firmware version changed?)"
        )
    data[BITRATE_OFFSET : BITRATE_OFFSET + 4] = dst
    p.write_bytes(bytes(data))
    print(f"   router: bitrate cap {BITRATE_SRC.hex()} -> {dst.hex()} ({kbps} kbps)")


def insert_gate(rootfs_dir: Path, home_macs: list[str]) -> None:
    gate = gate_template_path().read_text(encoding="utf-8")
    gate = gate.replace("\r\n", "\n").replace("\r", "\n")
    gate = gate.replace("%%HOME_MACS%%", " ".join(home_macs))
    if not gate.endswith("\n"):
        gate += "\n"
    sa = rootfs_dir / "etc" / "init.d" / "start_app"
    text = sa.read_text(encoding="utf-8")
    needle = "./recorder &"
    n = text.count(needle)
    if n != 1:
        die(
            f"start_app: expected exactly one '{needle}', found {n} "
            f"(firmware layout changed?)"
        )
    text = text.replace(needle + "\n", gate, 1)
    sa.write_text(text, encoding="utf-8", newline="\n")
    print(f"   start_app: recorder gated; baked home MACs: {' '.join(home_macs)}")


def verify_crc(out_path: str) -> bool:
    data = bytearray(Path(out_path).read_bytes())
    stored = struct.unpack("<Q", data[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 8])[0]
    data[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 8] = b"\x00" * 8
    return reolink_crc(bytes(data)) == stored


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Windows-native Reolink Duo 3 firmware patcher"
    )
    ap.add_argument("stock", help="stock .pak (your own download from Reolink)")
    ap.add_argument("out", help="output patched .pak")
    ap.add_argument("--kbps", type=int, default=20480, help="main-stream bitrate cap")
    ap.add_argument(
        "--home-mac",
        action="append",
        default=[],
        help="home gateway MAC to suppress recording on (repeatable)",
    )
    ap.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the record-at-home gate (HTTP unlock + bitrate only)",
    )
    ap.add_argument("--keep-work", action="store_true", help="keep the temp work dir")
    args = ap.parse_args()

    unsquashfs = find_tool("unsquashfs")
    mksquashfs = find_tool("mksquashfs")
    if not args.no_gate and not gate_template_path().exists():
        die(f"gate template not found: {gate_template_path()}")

    home_macs = [m.strip().lower() for m in args.home_mac if m.strip()]
    if not args.no_gate and not home_macs:
        mac = detect_home_mac()
        if not mac:
            die("could not auto-detect the home gateway MAC; pass --home-mac")
        print(f"Auto-detected home gateway MAC: {mac}")
        home_macs = [mac]

    stock = Path(args.stock).read_bytes()
    work = Path(tempfile.mkdtemp(prefix="d3patch_"))
    try:
        (work / "app.sqfs").write_bytes(section_blob(stock, "app"))
        (work / "rootfs.sqfs").write_bytes(section_blob(stock, "rootfs"))

        print("==> unsquashfs app + rootfs")
        run(
            [
                unsquashfs,
                "-d",
                str(work / "app"),
                "-no-progress",
                str(work / "app.sqfs"),
            ]
        )
        run(
            [
                unsquashfs,
                "-d",
                str(work / "rootfs"),
                "-no-progress",
                str(work / "rootfs.sqfs"),
            ]
        )

        print("==> patch app (device + router)")
        patch_device(work / "app")
        patch_router(work / "app", args.kbps)
        if not args.no_gate:
            print("==> insert record-at-home gate")
            insert_gate(work / "rootfs", home_macs)

        print("==> mksquashfs app + rootfs")
        run(
            [mksquashfs, str(work / "app"), str(work / "app_new.bin")] + MKSQUASHFS_ARGS
        )
        run(
            [mksquashfs, str(work / "rootfs"), str(work / "rootfs_new.bin")]
            + MKSQUASHFS_ARGS
        )

        print("==> repack pak + CRC")
        swaps = {
            "app": (work / "app_new.bin").read_bytes(),
            "rootfs": (work / "rootfs_new.bin").read_bytes(),
        }
        crc, size, secs = repack(args.stock, args.out, swaps=swaps)
        ok = verify_crc(args.out)
        print(f"wrote {args.out}  size={size}  crc=0x{crc:08x}  crc_ok={ok}")
        if not ok:
            die("CRC self-check failed — do not flash this image")
        print(
            "\nDone. UNVERIFIED on a camera — bench-test per "
            "docs/RECORD_GATE_DESIGN.md, then flash via the web UI "
            "(Settings -> Maintenance -> Local Upgrade)."
        )
    finally:
        if args.keep_work:
            print(f"work dir kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
