"""Clean Reolink PAK repacker — replaces one section in a stock PAK while
preserving the original byte layout (first section at 0x8c8) and recomputing
the correct Reolink CRC.

Why not just use pakler? Pakler computes the wrong CRC algorithm (it uses
zlib semantics) and silently shifts section payloads by -4 bytes (dropping
the original 4-byte zero pad before the loader section). The camera
hardcodes `read(fd, buf, 0x8c8)` for the header region, so a shifted
layout corrupts what the verifier computes the CRC over.

This packer:
  1. Reads the stock pak.
  2. Loads each section into memory.
  3. Optionally swaps in a replacement for one named section.
  4. Reassembles in the SAME order, starting payload at original first
     section's offset (0x8c8 in stock).
  5. Updates each section table entry's start/size to match new layout.
  6. Recomputes Reolink CRC and writes it at offset 0x08.

CLI:
  python pak_repack.py <stock.pak> <output.pak>
  python pak_repack.py <stock.pak> <output.pak> <section_name> <replacement_blob>

Examples:
  # No-op rebuild — output should be byte-identical to stock.
  python pak_repack.py stock.pak rebuilt.pak

  # Replace the app squashfs.
  python pak_repack.py stock.pak patched.pak app new_app.bin
"""

import struct
import sys

from reolink_crc import CRC_FIELD_OFFSET
from reolink_crc import compute as compute_reolink_crc

HEADER_SIZE = 0x18
ENTRY_SIZE = 0x48
NUM_TABLE_ENTRIES = 15  # full section table (8 used + 7 empty), per stock layout

# Sections that, if wrong, produce a camera that does not come back. Everything
# this project patches lives in `rootfs` and `app`, which are recoverable by
# reflashing; these five are not, without a UART cable.
BOOT_CHAIN = ("loader", "fdt", "atf", "uboot", "kernel")


class BootChainChanged(Exception):
    """A repack would have altered a section that has to survive byte-for-byte."""


def parse_section_table(data: bytes):
    """Return list of dicts for all 15 entries (empty ones included)."""
    entries = []
    for i in range(NUM_TABLE_ENTRIES):
        base = HEADER_SIZE + i * ENTRY_SIZE
        name = data[base : base + 0x20].split(b"\x00")[0].decode("ascii", "replace")
        ver = (
            data[base + 0x20 : base + 0x38].split(b"\x00")[0].decode("ascii", "replace")
        )
        off = struct.unpack("<Q", data[base + 0x38 : base + 0x40])[0]
        sz = struct.unpack("<Q", data[base + 0x40 : base + 0x48])[0]
        name_raw = data[base : base + 0x20]
        ver_raw = data[base + 0x20 : base + 0x38]
        entries.append(
            {
                "i": i,
                "name": name,
                "ver": ver,
                "off": off,
                "size": sz,
                "name_raw": name_raw,
                "ver_raw": ver_raw,
            }
        )
    return entries


def _assert_boot_chain(built: bytes, stock: bytes, what: str) -> list[str]:
    """Every BOOT_CHAIN section in `built` must equal the one in `stock`.

    Compared at each file's own advertised offsets, so this catches a section
    that was preserved but relocated as well as one that was modified.
    """
    stock_by_name = {s["name"]: s for s in parse_section_table(stock)}
    checked: list[str] = []
    for s in parse_section_table(built):
        if s["name"] not in BOOT_CHAIN or s["size"] == 0:
            continue
        ref = stock_by_name.get(s["name"])
        if ref is None or ref["size"] == 0:
            raise BootChainChanged(
                f"{what}: '{s['name']}' is absent from the stock pak"
            )
        if s["size"] != ref["size"]:
            raise BootChainChanged(
                f"{what}: '{s['name']}' size {s['size']} != stock {ref['size']}"
            )
        if s["off"] != ref["off"]:
            raise BootChainChanged(
                f"{what}: '{s['name']}' moved from 0x{ref['off']:x} to 0x{s['off']:x}"
            )
        if (
            built[s["off"] : s["off"] + s["size"]]
            != stock[ref["off"] : ref["off"] + ref["size"]]
        ):
            raise BootChainChanged(f"{what}: '{s['name']}' payload differs from stock")
        checked.append(s["name"])
    missing = [
        n
        for n in BOOT_CHAIN
        if n not in checked and n in stock_by_name and stock_by_name[n]["size"]
    ]
    if missing:
        raise BootChainChanged(f"{what}: sections {missing} vanished from the pak")
    return checked


def repack(
    stock_path: str,
    out_path: str,
    swap_name: str = None,
    swap_blob: bytes = None,
    swaps: dict = None,
):
    """Repack a stock pak, optionally swapping one or more sections.

    Either pass (swap_name, swap_blob) for a single swap, or `swaps={name: blob, ...}`
    for multiple. The two are mutually exclusive — `swaps` wins if both are given.
    """
    stock = open(stock_path, "rb").read()
    sections = parse_section_table(stock)
    used = [s for s in sections if s["size"] > 0]

    # Find original first-section offset and the bytes of the file before it
    # (this includes header + section table + partition table + any padding).
    first_off = min(s["off"] for s in used)
    head_region = stock[
        :first_off
    ]  # everything up to and including padding before sections

    # Build the swap map.
    swap_map: dict = {}
    if swaps:
        swap_map.update(swaps)
    elif swap_name is not None and swap_blob is not None:
        swap_map[swap_name] = swap_blob

    # Pull each used section's bytes (or replacement).
    new_blobs = {}
    for s in used:
        if s["name"] in swap_map:
            new_blobs[s["name"]] = swap_map[s["name"]]
        else:
            new_blobs[s["name"]] = stock[s["off"] : s["off"] + s["size"]]

    # The boot chain must survive byte-for-byte, and the check goes here --
    # before a single byte is assembled and long before the CRC is computed --
    # so that a build which would have moved it cannot produce a file at all.
    # A pak with a valid CRC and a damaged loader is exactly the artifact you
    # cannot tell apart from a good one until you have already flashed it.
    for s in used:
        if s["name"] not in BOOT_CHAIN:
            continue
        original = stock[s["off"] : s["off"] + s["size"]]
        if new_blobs[s["name"]] != original:
            raise BootChainChanged(
                f"section '{s['name']}' would change "
                f"({len(original)} -> {len(new_blobs[s['name']])} bytes). "
                "Only 'rootfs' and 'app' may be replaced."
            )

    # Rebuild section table entries in same order, recompute offsets/sizes.
    out = bytearray(head_region)  # preserve header geometry exactly
    cursor = first_off  # payload starts here, same as original

    new_meta = {}
    for s in used:
        blob = new_blobs[s["name"]]
        new_meta[s["i"]] = (cursor, len(blob))
        out += blob
        cursor += len(blob)

    # Patch each entry's start/size in the rebuilt header (entries we updated).
    for i, (off, size) in new_meta.items():
        base = HEADER_SIZE + i * ENTRY_SIZE
        out[base + 0x38 : base + 0x40] = struct.pack("<Q", off)
        out[base + 0x40 : base + 0x48] = struct.pack("<Q", size)

    # Empty entries (i not in new_meta) — leave them as-is from head_region.

    # Assert it again against the ASSEMBLED image, at the offsets the new
    # section table advertises. The check above proves the payloads are equal;
    # this one proves the layout did not move them, which is a different bug
    # and one the CRC would happily bless.
    _assert_boot_chain(bytes(out), stock, "assembled image")

    # Recompute and write Reolink CRC.
    out[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 8] = b"\x00" * 8
    crc = compute_reolink_crc(bytes(out))
    out[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 8] = struct.pack("<Q", crc)

    open(out_path, "wb").write(out)
    return (
        crc,
        len(out),
        [(s["name"], new_meta[s["i"]][0], new_meta[s["i"]][1]) for s in used],
    )


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--check-boot-chain":
        stock_b = open(sys.argv[2], "rb").read()
        built_b = open(sys.argv[3], "rb").read()
        try:
            names = _assert_boot_chain(built_b, stock_b, sys.argv[3])
        except BootChainChanged as exc:
            print(f"REFUSING: {exc}")
            sys.exit(1)
        print(f"PASS: boot chain byte-identical to stock ({', '.join(names)})")
        sys.exit(0)

    if len(sys.argv) < 3:
        print(
            "usage: pak_repack.py <stock.pak> <output.pak> [section_name replacement_blob]\n"
            "       pak_repack.py --check-boot-chain <stock.pak> <built.pak>"
        )
        sys.exit(2)

    stock = sys.argv[1]
    out = sys.argv[2]
    swap_name = sys.argv[3] if len(sys.argv) >= 5 else None
    swap_blob = open(sys.argv[4], "rb").read() if len(sys.argv) >= 5 else None

    crc, size, secs = repack(stock, out, swap_name, swap_blob)
    print(f"wrote {out}  size={size}  crc=0x{crc:08x}")
    for name, off, sz in secs:
        marker = "  (replaced)" if name == swap_name else ""
        print(f"  {name:10s} start=0x{off:08x} size=0x{sz:08x}{marker}")
