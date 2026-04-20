import struct
import os

PAK = "IPC_NT15NA416MP.4867_2505072124.Reolink-Duo-3-PoE.16MP.REOLINK.pak"
OUT = "sections"
os.makedirs(OUT, exist_ok=True)

data = open(PAK, "rb").read()
print("file size: 0x%x" % len(data))
print("header magic:", data[:4].hex())
print(
    "hdr field@0x04 (8B):",
    data[0x04:0x0C].hex(),
    "=>",
    struct.unpack("<Q", data[0x04:0x0C])[0],
)
print(
    "hdr field@0x10 (4B):",
    data[0x10:0x14].hex(),
    "=>",
    struct.unpack("<I", data[0x10:0x14])[0],
)

for i in range(8):
    base = 0x18 + i * 0x48
    name = data[base : base + 0x20].split(b"\x00")[0].decode(errors="replace")
    ver = data[base + 0x20 : base + 0x38].split(b"\x00")[0].decode(errors="replace")
    off = struct.unpack("<Q", data[base + 0x38 : base + 0x40])[0]
    sz = struct.unpack("<Q", data[base + 0x40 : base + 0x48])[0]
    blob = data[off : off + sz]
    out = os.path.join(OUT, "%d_%s.bin" % (i, name))
    open(out, "wb").write(blob)
    print("wrote %-30s off=0x%08x size=0x%08x (%d) ver=%s" % (out, off, sz, sz, ver))

# After section table there is the "partition table" section starting around 0x450
print()
print("hex 0x450..0x6f0 (partition map):")
print(data[0x450:0x6F0].hex())
