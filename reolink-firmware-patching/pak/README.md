# Reolink PAK container toolchain

Low-level tools for parsing, extracting, and repacking Reolink `.pak`
firmware images. This is the foundation the `../build/*.sh` patchers
build on.

## Files

| File | Purpose |
|---|---|
| `pak.py` | Parse and inspect a `.pak`. `python pak.py <pak_file>` prints the section table and verifies the top-level header checksum. Pure stdlib. |
| `extract.py` | Split a `.pak` into its sections as `<out_dir>/<index>_<name>.bin`. |
| `pak_repack.py` | Build a new `.pak` from a stock one plus a replacement for a single section. Preserves the exact byte layout the camera's verifier expects (first section at `0x8c8`, full 15-entry section table, correct CRC). Usage: `python pak_repack.py <stock.pak> <out.pak> <section_name> <new_payload.bin>`. |
| `reolink_crc.py` | Standalone CRC compute / rewrite. `compute` prints both stored-in-file and freshly-computed CRCs; `patch` recomputes and writes the correct value back into the header. Used internally by `pak_repack.py`; exposed here for standalone verification. |

## The CRC algorithm (non-standard)

The existing `vmallet/pakler` tool uses standard zlib CRC32 semantics and
gets a different value than the camera computes. The actual algorithm,
reverse-engineered from the `upgrade` ELF inside the `app` section
(function `bc_gen_crc` at VMA `0x4195b8`):

```c
uint64_t bc_gen_crc(uint64_t init, const char *data, uint64_t len) {
    while (len--) {
        init = TABLE[(init ^ *data++) & 0xff] ^ (init >> 8);
    }
    return init;
}
```

The 256-entry table is the standard zlib CRC32 polynomial `0xedb88320`,
but stored as 8-byte cells (upper 32 bits zero). **Init = 0, no final
XOR.** The verifier hashes payload[`0x8c8`:] + an 8-byte type marker
`\x02\x00...` + the full 15-entry section table, and compares against
the u32 at offset `0x08` in the header.

`reolink_crc.py` implements this and matches the stock pak's stored
value byte-for-byte.

## Safety check

Round-trip a stock pak through `pak_repack.py` with no changes:
```bash
python pak_repack.py stock.pak roundtrip.pak app same_app_section.bin
diff <(sha256sum stock.pak | cut -c1-64) <(sha256sum roundtrip.pak | cut -c1-64)
```
Expect byte-identical output.
