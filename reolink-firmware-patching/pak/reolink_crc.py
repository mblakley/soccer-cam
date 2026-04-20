"""Compute and patch Reolink .pak header CRC (the field at file offset 0x08).

Reverse-engineered from /mnt/app/upgrade in firmware
v3.0.0.4867_2505072124 (Duo 3 PoE). The relevant function is at VMA
0x41af80; the CRC algorithm itself (`bc_gen_crc`) is the symbol
`_Z10bc_gen_crcmPKcm` at VMA 0x4195b8 with its 256-entry table at VMA
0x4540d0 — table contents are the standard zlib polynomial 0xedb88320
in 64-bit cells (upper 32 bits zero). However, the wrapping is NOT
zlib's: init=0, no final XOR.

Algorithm (matches stored CRC of stock pak byte-for-byte):

    crc = bc_gen_crc(0, payload[0x8c8:])                  # all bytes from first section to EOF
    crc = bc_gen_crc(crc, b'\\x02' + b'\\x00'*7)           # 8-byte type marker for PAK64
    crc = bc_gen_crc(crc, header[0x18 : 0x18 + 15*0x48])  # full 15-entry section table
    return crc & 0xffffffff

Pakler's calc_crc uses zlib semantics + only 4 bytes of "2" + only the
used section entries — wrong on three counts. Use this module to
correct pakler's output before flashing.

Usage as CLI:
    python reolink_crc.py compute  <pak_path>
    python reolink_crc.py patch    <pak_path>      # rewrites bytes 0x08..0x0c in place
"""

import struct
import sys


# Standard zlib CRC-32 polynomial (0xedb88320), reflected.
def _build_table():
    poly = 0xEDB88320
    t = [0] * 256
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else (c >> 1)
        t[i] = c
    return t


TABLE = _build_table()


def bc_gen_crc(init: int, data: bytes) -> int:
    """Reolink's bc_gen_crc: standard CRC-32 polynomial table walk, init=0,
    no final inversion. Equivalent to ~zlib.crc32(data) only when init = ~0."""
    crc = init
    for b in data:
        crc = TABLE[(b ^ crc) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF


# Reolink pak header geometry (PAK64).
HEADER_SIZE = 0x18
SECTION_ENTRY_SIZE = 0x48
SECTION_TABLE_ENTRIES = 15  # full table (8 used + 7 empty)
SECTION_TABLE_BYTES = SECTION_ENTRY_SIZE * SECTION_TABLE_ENTRIES  # 0x438
TYPE_MARKER = b"\x02" + b"\x00" * 7  # 8 bytes; PAKType.PAK64 = 2
CRC_FIELD_OFFSET = 0x08  # 4 effective bytes, stored as LE u64 (upper 4 = 0)


def first_section_offset(data: bytes) -> int:
    """Return the file offset of the first byte of section payload data
    (i.e. the smallest section.start in the section table)."""
    starts = []
    for i in range(SECTION_TABLE_ENTRIES):
        base = HEADER_SIZE + i * SECTION_ENTRY_SIZE
        sz = struct.unpack("<Q", data[base + 0x40 : base + 0x48])[0]
        if sz:
            off = struct.unpack("<Q", data[base + 0x38 : base + 0x40])[0]
            starts.append(off)
    return min(starts)


def compute(data: bytes) -> int:
    """Compute the Reolink pak header CRC over the given .pak file bytes."""
    payload_off = first_section_offset(data)
    crc = bc_gen_crc(0, data[payload_off:])
    crc = bc_gen_crc(crc, TYPE_MARKER)
    crc = bc_gen_crc(crc, data[HEADER_SIZE : HEADER_SIZE + SECTION_TABLE_BYTES])
    return crc


def patch_in_place(path: str) -> tuple[int, int]:
    """Recompute CRC, write it to the file at offset 0x08. Returns (old, new)."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    old = struct.unpack("<I", data[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 4])[0]
    new = compute(bytes(data))
    data[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 8] = struct.pack("<Q", new)
    with open(path, "wb") as f:
        f.write(data)
    return old, new


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compute"
    path = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak"
    )
    data = open(path, "rb").read()
    stored = struct.unpack("<I", data[CRC_FIELD_OFFSET : CRC_FIELD_OFFSET + 4])[0]
    computed = compute(data)
    print(f"file:     {path}")
    print(f"stored:   0x{stored:08x}")
    print(f"computed: 0x{computed:08x}")
    print(f"match:    {stored == computed}")
    if cmd == "patch":
        old, new = patch_in_place(path)
        print(f"patched in place: 0x{old:08x} -> 0x{new:08x}")
