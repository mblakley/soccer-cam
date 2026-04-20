"""Reolink .pak parse/extract/rebuild library.

PAK layout (Novatek/Reolink):
  0x00..0x03  magic = 13 59 72 32  (LE 0x32725913)
  0x04..0x0b  checksum field (8 bytes). Empirically the upper 32 bits hold a
              CRC32 over the file with this field zeroed; lower 32 bits zero.
  0x0c..0x0f  unknown / flags     (observed 0)
  0x10..0x13  section count or fmt (observed 0x4b02 = 19202)
  0x14..0x17  padding             (observed 0)
  0x18..    8 section entries, 0x48 bytes each:
                0x00..0x1f  name (NUL-padded ascii, e.g. "loader")
                0x20..0x37  version (e.g. "v1.0.0.1")
                0x38..0x3f  file offset (LE u64)
                0x40..0x47  section size in bytes (LE u64)
  Then a "partition table" of 8 entries (~0x48 each) starting around 0x450:
                name, /dev/mtdN, flash_offset (u32), flash_size (u32)
  Section payload region begins at the smallest offset in the section table
  (loader's 0x8c8 in the stock pak).
"""

import os
import struct
import zlib

MAGIC = bytes.fromhex("13597232")
ENTRY_BASE = 0x18
ENTRY_SIZE = 0x48
NUM_SECTIONS = 8


def parse(data: bytes):
    if data[:4] != MAGIC:
        raise ValueError(f"bad magic {data[:4].hex()}")
    sections = []
    for i in range(NUM_SECTIONS):
        base = ENTRY_BASE + i * ENTRY_SIZE
        name = data[base : base + 0x20].split(b"\x00")[0].decode("ascii", "replace")
        ver = (
            data[base + 0x20 : base + 0x38].split(b"\x00")[0].decode("ascii", "replace")
        )
        off = struct.unpack("<Q", data[base + 0x38 : base + 0x40])[0]
        sz = struct.unpack("<Q", data[base + 0x40 : base + 0x48])[0]
        sections.append({"i": i, "name": name, "ver": ver, "offset": off, "size": sz})
    return sections


def header_region_size(sections):
    """Bytes from 0 up to (but not including) the first section payload."""
    return min(s["offset"] for s in sections if s["size"])


def extract(pak_path: str, out_dir: str):
    """Extract every section to <out_dir>/<i>_<name>.bin and dump the header."""
    data = open(pak_path, "rb").read()
    secs = parse(data)
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "_header.bin"), "wb").write(
        data[: header_region_size(secs)]
    )
    for s in secs:
        path = os.path.join(out_dir, f"{s['i']}_{s['name']}.bin")
        open(path, "wb").write(data[s["offset"] : s["offset"] + s["size"]])
    return secs


def compute_checksum(data_with_zeroed_field: bytes) -> int:
    """CRC32 over the entire file with bytes 0x04..0x0b set to zero."""
    return zlib.crc32(data_with_zeroed_field) & 0xFFFFFFFF


def write_checksum(data: bytearray, crc: int):
    """Stored as little-endian u64; lower 32 = 0, upper 32 = CRC."""
    data[0x04:0x0C] = struct.pack("<Q", crc << 32)


def build(header: bytes, section_blobs: dict) -> bytes:
    """Rebuild a .pak from a stock header and a dict of {name: bytes}.

    The header retains the existing layout (entry count, partition table,
    versions). Section payloads are placed back-to-back starting at the
    same offset as in the original (header_region_size). Offsets and sizes
    in the section table are rewritten to match the new payloads.
    Checksum is recomputed last.
    """
    secs = parse(
        header + b"\x00" * 0x10000
    )  # parse needs space; we only read header bytes
    # Rebuild section table with new offsets/sizes, keeping name+ver.
    out = bytearray(header)
    cursor = header_region_size(secs)

    new_sections = []
    payload_chunks = []
    for s in secs:
        blob = section_blobs.get(s["name"])
        if blob is None:
            raise KeyError(f"missing section '{s['name']}' in build()")
        new_sections.append({**s, "offset": cursor, "size": len(blob)})
        payload_chunks.append(blob)
        cursor += len(blob)

    # Patch table entries in-place
    for s in new_sections:
        base = ENTRY_BASE + s["i"] * ENTRY_SIZE
        out[base + 0x38 : base + 0x40] = struct.pack("<Q", s["offset"])
        out[base + 0x40 : base + 0x48] = struct.pack("<Q", s["size"])

    # Append payloads
    out += b"".join(payload_chunks)

    # Zero the checksum field, compute, then write
    out[0x04:0x0C] = b"\x00" * 8
    crc = compute_checksum(bytes(out))
    write_checksum(out, crc)
    return bytes(out)


def probe_checksum(pak_path: str):
    """Diagnostic — try several CRC variants against the stored value."""
    data = open(pak_path, "rb").read()
    stored64 = struct.unpack("<Q", data[0x04:0x0C])[0]
    print(f"stored u64 @0x04 = 0x{stored64:016x}")
    print(f"  lower 32       = 0x{stored64 & 0xFFFFFFFF:08x}")
    print(f"  upper 32       = 0x{(stored64 >> 32) & 0xFFFFFFFF:08x}")

    zeroed = bytearray(data)
    zeroed[0x04:0x0C] = b"\x00" * 8
    candidates = [
        ("CRC32 full, 0x04..0x0c zeroed", zlib.crc32(bytes(zeroed))),
        ("CRC32 of data[0x0c:]", zlib.crc32(data[0x0C:])),
        ("CRC32 of data[0x10:]", zlib.crc32(data[0x10:])),
        ("CRC32 of data[0x18:]", zlib.crc32(data[0x18:])),
        (
            "CRC32 of header_region only",
            zlib.crc32(bytes(zeroed[: header_region_size(parse(data))])),
        ),
    ]
    upper = (stored64 >> 32) & 0xFFFFFFFF
    lower = stored64 & 0xFFFFFFFF
    for name, val in candidates:
        v = val & 0xFFFFFFFF
        tag = ""
        if v == upper:
            tag = "  <-- matches upper 32 of stored"
        if v == lower:
            tag = "  <-- matches lower 32 of stored"
        print(f"  {v:08x}  {name}{tag}")


if __name__ == "__main__":
    import sys

    pak = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak"
    )
    data = open(pak, "rb").read()
    print(f"PAK: {pak}  size=0x{len(data):x}")
    for s in parse(data):
        print(
            f"  [{s['i']}] {s['name']:8s} v{s['ver']:18s}  off=0x{s['offset']:09x}  size=0x{s['size']:09x}"
        )
    print()
    probe_checksum(pak)
