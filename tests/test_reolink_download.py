"""Tests for Baichuan protocol download (BcMediaDemuxer + BaichuanStreamClient)."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video_grouper.cameras.reolink_download import (
    ANNEX_B_START_CODE,
    HEADER_MAGIC,
    HOST_CH_ID,
    IFRAME_ENCRYPT_BOUNDARY,
    MAGIC_AAC,
    MAGIC_ADPCM,
    MAGIC_IFRAME_END,
    MAGIC_IFRAME_START,
    MAGIC_INFO_V1,
    MAGIC_INFO_V2,
    MAGIC_PFRAME_END,
    MAGIC_PFRAME_START,
    MSG_CLASS_LEGACY,
    PAD_SIZE,
    BaichuanStreamClient,
    BcMediaDemuxer,
    _aes_decrypt,
    _aes_encrypt,
    _decrypt_baichuan,
    _encrypt_baichuan,
    _has_start_codes,
    _is_known_magic,
    _length_prefixed_to_annex_b,
    _md5_str_modern,
    _to_annex_b,
)


# ── Helper: build synthetic BcMedia packets ──────────────────────────


def _build_info_v1(width=1920, height=1080, fps=25):
    """Build a 32-byte BcMedia InfoV1 header."""
    buf = bytearray(32)
    struct.pack_into("<I", buf, 0, MAGIC_INFO_V1)
    struct.pack_into("<I", buf, 4, 32)  # header_size
    struct.pack_into("<I", buf, 8, width)
    struct.pack_into("<I", buf, 12, height)
    buf[17] = fps
    return bytes(buf)


def _build_video_frame(frame_type="iframe", codec="H265", payload=b"\x00" * 16):
    """Build a BcMedia I-frame or P-frame packet."""
    magic = MAGIC_IFRAME_START if frame_type == "iframe" else MAGIC_PFRAME_START
    codec_bytes = codec.encode("ascii")[:4]
    additional_header_size = 4  # minimal additional header
    additional_header = b"\x00" * additional_header_size
    microseconds = 0
    unknown = 0

    payload_size = len(payload)
    pad = 0 if payload_size % PAD_SIZE == 0 else PAD_SIZE - (payload_size % PAD_SIZE)

    buf = bytearray()
    buf += struct.pack("<I", magic)
    buf += codec_bytes
    buf += struct.pack("<I", payload_size)
    buf += struct.pack("<I", additional_header_size)
    buf += struct.pack("<I", microseconds)
    buf += struct.pack("<I", unknown)
    buf += additional_header
    buf += payload
    buf += b"\x00" * pad
    return bytes(buf)


def _build_aac_frame(payload=b"\xaa" * 8):
    """Build a BcMedia AAC audio packet."""
    size = len(payload)
    pad = 0 if size % PAD_SIZE == 0 else PAD_SIZE - (size % PAD_SIZE)
    buf = bytearray()
    buf += struct.pack("<I", MAGIC_AAC)
    buf += struct.pack("<H", size)
    buf += struct.pack("<H", size)
    buf += payload
    buf += b"\x00" * pad
    return bytes(buf)


def _build_adpcm_frame(block_data=b"\xbb" * 8):
    """Build a BcMedia ADPCM audio packet."""
    sub_header_size = 4
    payload_size = sub_header_size + len(block_data)
    pad = 0 if payload_size % PAD_SIZE == 0 else PAD_SIZE - (payload_size % PAD_SIZE)
    buf = bytearray()
    buf += struct.pack("<I", MAGIC_ADPCM)
    buf += struct.pack("<H", payload_size)
    buf += struct.pack("<H", payload_size)
    buf += struct.pack("<H", 0x0100)  # magic_data
    buf += struct.pack("<H", len(block_data) // 2)  # half_block_size
    buf += block_data
    buf += b"\x00" * pad
    return bytes(buf)


# ── Crypto helper tests ──────────────────────────────────────────────


class TestCryptoHelpers:
    def test_md5_str_modern_format(self):
        result = _md5_str_modern("test")
        assert len(result) == 31
        assert result == result.upper()
        assert all(c in "0123456789ABCDEF" for c in result)

    def test_md5_str_modern_deterministic(self):
        assert _md5_str_modern("hello") == _md5_str_modern("hello")
        assert _md5_str_modern("a") != _md5_str_modern("b")

    def test_baichuan_roundtrip(self):
        original = "Hello, World! This is a test XML payload."
        for offset in (0, 1, 42, 250, 255):
            encrypted = _encrypt_baichuan(original, offset)
            decrypted = _decrypt_baichuan(encrypted, offset)
            assert decrypted == original, f"Failed for offset={offset}"

    def test_aes_roundtrip(self):
        key = b"0123456789abcdef"
        original = b"test data that is at least 16 bytes long!!"
        encrypted = _aes_encrypt(original, key)
        decrypted = _aes_decrypt(encrypted, key)
        assert decrypted == original

    def test_aes_different_keys_differ(self):
        data = b"secret message!!"
        enc_a = _aes_encrypt(data, b"keyAAAAAAAAAAAAA")
        enc_b = _aes_encrypt(data, b"keyBBBBBBBBBBBBB")
        assert enc_a != enc_b

    def test_is_known_magic(self):
        assert _is_known_magic(MAGIC_INFO_V1)
        assert _is_known_magic(MAGIC_INFO_V2)
        assert _is_known_magic(MAGIC_IFRAME_START)
        assert _is_known_magic(MAGIC_IFRAME_END)
        assert _is_known_magic(MAGIC_PFRAME_START)
        assert _is_known_magic(MAGIC_PFRAME_END)
        assert _is_known_magic(MAGIC_AAC)
        assert _is_known_magic(MAGIC_ADPCM)
        assert not _is_known_magic(0x00000000)
        assert not _is_known_magic(0xDEADBEEF)


# ── NAL conversion tests ────────────────────────────────────────────


class TestNalConversion:
    def test_has_start_codes_4byte(self):
        assert _has_start_codes(b"\x00\x00\x00\x01\x67")

    def test_has_start_codes_3byte(self):
        assert _has_start_codes(b"\x00\x00\x01\x67")

    def test_has_start_codes_false(self):
        assert not _has_start_codes(b"\x00\x00\x00\x10")
        assert not _has_start_codes(b"\x01\x02\x03\x04")

    def test_length_prefixed_to_annex_b_single(self):
        nal_data = b"\x67\x42\x00\x1e"
        lp = struct.pack(">I", len(nal_data)) + nal_data
        result = _length_prefixed_to_annex_b(lp)
        assert result == ANNEX_B_START_CODE + nal_data

    def test_length_prefixed_to_annex_b_multiple(self):
        nal1 = b"\x67\x42"
        nal2 = b"\x68\x01\x02"
        lp = struct.pack(">I", len(nal1)) + nal1 + struct.pack(">I", len(nal2)) + nal2
        result = _length_prefixed_to_annex_b(lp)
        assert result == ANNEX_B_START_CODE + nal1 + ANNEX_B_START_CODE + nal2

    def test_length_prefixed_to_annex_b_zero_length(self):
        lp = struct.pack(">I", 0)
        result = _length_prefixed_to_annex_b(lp)
        assert result == b""

    def test_to_annex_b_already_annexb(self):
        data = b"\x00\x00\x00\x01\x67\x42\x00\x1e"
        assert _to_annex_b(data) is data  # Should return the same object

    def test_to_annex_b_converts_lp(self):
        nal = b"\x67\x42\x00\x1e"
        lp = struct.pack(">I", len(nal)) + nal
        result = _to_annex_b(lp)
        assert result == ANNEX_B_START_CODE + nal

    def test_to_annex_b_short_data(self):
        data = b"\x01\x02"
        assert _to_annex_b(data) == data


# ── BcMediaDemuxer tests ────────────────────────────────────────────


class TestBcMediaDemuxer:
    def test_parse_info_v1(self):
        demuxer = BcMediaDemuxer()
        packet = _build_info_v1(width=4096, height=1800, fps=30)
        frames = demuxer.feed(packet)
        assert len(frames) == 1
        assert frames[0][0] == "info"
        assert demuxer.width == 4096
        assert demuxer.height == 1800
        assert demuxer.fps == 30

    def test_parse_info_v2(self):
        demuxer = BcMediaDemuxer()
        buf = bytearray(32)
        struct.pack_into("<I", buf, 0, MAGIC_INFO_V2)
        struct.pack_into("<I", buf, 4, 32)
        struct.pack_into("<I", buf, 8, 1920)
        struct.pack_into("<I", buf, 12, 1080)
        buf[17] = 25
        frames = demuxer.feed(bytes(buf))
        assert len(frames) == 1
        assert frames[0][0] == "info"
        assert demuxer.width == 1920

    def test_parse_iframe(self):
        demuxer = BcMediaDemuxer()
        payload = b"\xaa" * 32
        packet = _build_video_frame("iframe", "H265", payload)
        frames = demuxer.feed(packet)
        assert len(frames) == 1
        frame_type, codec, data = frames[0]
        assert frame_type == "iframe"
        assert codec == "H265"
        assert data == payload

    def test_parse_pframe(self):
        demuxer = BcMediaDemuxer()
        payload = b"\xbb" * 16
        packet = _build_video_frame("pframe", "H264", payload)
        frames = demuxer.feed(packet)
        assert len(frames) == 1
        frame_type, codec, data = frames[0]
        assert frame_type == "pframe"
        assert codec == "H264"
        assert data == payload

    def test_parse_aac(self):
        demuxer = BcMediaDemuxer()
        aac_data = b"\xcc" * 12
        packet = _build_aac_frame(aac_data)
        frames = demuxer.feed(packet)
        assert len(frames) == 1
        frame_type, codec, data = frames[0]
        assert frame_type == "aac"
        assert data == aac_data

    def test_parse_adpcm(self):
        demuxer = BcMediaDemuxer()
        block_data = b"\xdd" * 16
        packet = _build_adpcm_frame(block_data)
        frames = demuxer.feed(packet)
        assert len(frames) == 1
        frame_type, codec, data = frames[0]
        assert frame_type == "adpcm"
        assert data == block_data

    def test_multiple_packets_at_once(self):
        demuxer = BcMediaDemuxer()
        data = (
            _build_info_v1()
            + _build_video_frame("iframe", "H265", b"\x01" * 16)
            + _build_video_frame("pframe", "H265", b"\x02" * 8)
        )
        frames = demuxer.feed(data)
        assert len(frames) == 3
        assert frames[0][0] == "info"
        assert frames[1][0] == "iframe"
        assert frames[2][0] == "pframe"

    def test_incremental_feed(self):
        """Test feeding data in small chunks (fragmented across calls)."""
        demuxer = BcMediaDemuxer()
        packet = _build_video_frame("iframe", "H265", b"\x55" * 24)

        # Feed in 10-byte chunks
        all_frames = []
        for i in range(0, len(packet), 10):
            chunk = packet[i : i + 10]
            all_frames.extend(demuxer.feed(chunk))

        assert len(all_frames) == 1
        assert all_frames[0][0] == "iframe"
        assert all_frames[0][2] == b"\x55" * 24

    def test_resync_on_garbage(self):
        """Test that demuxer resyncs after garbage data."""
        demuxer = BcMediaDemuxer()
        garbage = b"\xff" * 20
        valid = _build_video_frame("iframe", "H264", b"\x01" * 8)
        frames = demuxer.feed(garbage + valid)
        assert len(frames) == 1
        assert frames[0][0] == "iframe"

    def test_incomplete_packet_buffered(self):
        """Test that incomplete packets are buffered for the next feed."""
        demuxer = BcMediaDemuxer()
        packet = _build_video_frame("iframe", "H265", b"\x01" * 16)
        # Feed only half
        half = len(packet) // 2
        frames = demuxer.feed(packet[:half])
        assert len(frames) == 0  # Not enough data yet
        # Feed the rest
        frames = demuxer.feed(packet[half:])
        assert len(frames) == 1

    def test_codec_tracked(self):
        demuxer = BcMediaDemuxer()
        demuxer.feed(_build_video_frame("iframe", "H265", b"\x01" * 8))
        assert demuxer.video_codec == "H265"
        demuxer.feed(_build_video_frame("pframe", "H264", b"\x02" * 8))
        assert demuxer.video_codec == "H264"

    def test_empty_feed(self):
        demuxer = BcMediaDemuxer()
        assert demuxer.feed(b"") == []

    def test_padding_alignment(self):
        """Verify frames with various payload sizes and their padding."""
        for payload_len in (1, 7, 8, 9, 15, 16, 17, 24, 25):
            demuxer = BcMediaDemuxer()
            payload = bytes(range(payload_len))
            packet = _build_video_frame("iframe", "H265", payload)
            frames = demuxer.feed(packet)
            assert len(frames) == 1, f"Failed for payload_len={payload_len}"
            assert frames[0][2] == payload


# ── BaichuanStreamClient tests ───────────────────────────────────────


def _make_client():
    return BaichuanStreamClient("192.168.1.200", 9000, "admin", "password123")


class TestBaichuanStreamClientHeaders:
    def test_build_header_length(self):
        client = _make_client()
        hdr = client._build_header(cmd_id=5, body_length=100)
        assert len(hdr) == 24
        assert hdr[:4] == HEADER_MAGIC

    def test_build_header_legacy_length(self):
        client = _make_client()
        hdr = client._build_header_legacy(cmd_id=1, body_length=0)
        assert len(hdr) == 20
        assert hdr[:4] == HEADER_MAGIC

    def test_build_header_cmd_id(self):
        client = _make_client()
        hdr = client._build_header(cmd_id=13, body_length=0)
        cmd_id = struct.unpack_from("<I", hdr, 4)[0]
        assert cmd_id == 13

    def test_build_header_body_length(self):
        client = _make_client()
        hdr = client._build_header(cmd_id=5, body_length=12345)
        body_len = struct.unpack_from("<I", hdr, 8)[0]
        assert body_len == 12345

    def test_build_header_message_class(self):
        client = _make_client()
        hdr = client._build_header(cmd_id=5, body_length=0, message_class="1464")
        # messageClass at bytes 18-19 as raw hex bytes
        assert hdr[18:20] == bytes.fromhex("1464")

    def test_build_header_payload_offset(self):
        client = _make_client()
        hdr = client._build_header(cmd_id=5, body_length=100, payload_offset=42)
        po = struct.unpack_from("<I", hdr, 20)[0]
        assert po == 42

    def test_msg_num_increments(self):
        client = _make_client()
        hdr1 = client._build_header(cmd_id=5, body_length=0)
        hdr2 = client._build_header(cmd_id=5, body_length=0)
        # msgNum is at bytes 14-15 (u16 LE)
        id1 = struct.unpack_from("<H", hdr1, 14)[0]
        id2 = struct.unpack_from("<H", hdr2, 14)[0]
        assert id2 == id1 + 1


class TestBaichuanStreamClientDecryption:
    def test_decrypt_unencrypted_chunk(self):
        """Raw BcMedia data (not encrypted) should pass through."""
        client = _make_client()
        client._aes_key = b"0123456789abcdef"

        raw = _build_info_v1()
        result = client._decrypt_stream_chunk(raw)
        assert result == raw

    def test_decrypt_fully_encrypted_small(self):
        """Small fully-encrypted chunk should be AES-decrypted."""
        client = _make_client()
        client._aes_key = b"0123456789abcdef"

        raw = _build_info_v1()
        encrypted = _aes_encrypt(raw, client._aes_key)
        result = client._decrypt_stream_chunk(encrypted)

        # Should detect BcMedia magic after decryption
        magic = struct.unpack_from("<I", result, 0)[0]
        assert _is_known_magic(magic)

    def test_decrypt_iframe_partial(self):
        """I-frame >1024 bytes: only first 1024 bytes should be decrypted."""
        client = _make_client()
        client._aes_key = b"0123456789abcdef"

        # Build a large "BcMedia-like" buffer
        payload = _build_video_frame("iframe", "H265", b"\x42" * 2000)
        assert len(payload) > IFRAME_ENCRYPT_BOUNDARY

        # Encrypt only the first 1024 bytes (simulating camera behavior)
        enc_head = _aes_encrypt(payload[:IFRAME_ENCRYPT_BOUNDARY], client._aes_key)
        encrypted = enc_head + payload[IFRAME_ENCRYPT_BOUNDARY:]

        result = client._decrypt_stream_chunk(encrypted)
        # The decrypted result should start with the original BcMedia magic
        assert result[:IFRAME_ENCRYPT_BOUNDARY] == payload[:IFRAME_ENCRYPT_BOUNDARY]
        assert result[IFRAME_ENCRYPT_BOUNDARY:] == payload[IFRAME_ENCRYPT_BOUNDARY:]

    def test_decrypt_empty(self):
        client = _make_client()
        client._aes_key = b"0123456789abcdef"
        assert client._decrypt_stream_chunk(b"") == b""

    def test_decrypt_no_key(self):
        client = _make_client()
        client._aes_key = None
        data = b"\x01\x02\x03\x04"
        assert client._decrypt_stream_chunk(data) == data


class TestBaichuanStreamClientLogin:
    @pytest.mark.asyncio
    async def test_login_success(self):
        """Test the full login flow with mocked TCP."""
        client = _make_client()

        # Build mock nonce response
        nonce_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            "<body>"
            "<Encryption>"
            "<nonce>TESTNONCE123</nonce>"
            "</Encryption>"
            "</body>"
        )
        nonce_body = _encrypt_baichuan(nonce_xml, HOST_CH_ID)
        nonce_header = (
            HEADER_MAGIC
            + struct.pack("<I", 1)  # cmd_id
            + struct.pack("<I", len(nonce_body))  # body_length
            + struct.pack("<B", HOST_CH_ID)  # ch_id
            + struct.pack("<I", 1)[:3]  # mess_id
            + bytes.fromhex("0000" + MSG_CLASS_LEGACY)
        )

        # Build mock login success response (modern 24-byte header)
        login_resp_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            "<body>"
            '<DeviceInfo version="1.1">'
            "<uid>ABCDEF1234567890</uid>"
            "</DeviceInfo>"
            "</body>"
        )
        login_body = _encrypt_baichuan(login_resp_xml, HOST_CH_ID)
        login_header = (
            HEADER_MAGIC
            + struct.pack("<I", 1)  # cmd_id
            + struct.pack("<I", len(login_body))  # body_length
            + struct.pack("<B", HOST_CH_ID)  # channelId
            + struct.pack("<B", 0)  # streamType
            + struct.pack("<H", 2)  # msgNum
            + b"\xc8\x00"  # responseCode=200 (LE u16)
            + b"\x00\x00"  # messageClass=0x0000 (24-byte)
            + struct.pack("<I", 0)  # payloadOffset
        )

        # Concatenate all response data
        response_data = nonce_header + nonce_body + login_header + login_body

        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.is_closing = MagicMock(return_value=False)

        # Track read position
        read_pos = [0]

        async def mock_readexactly(n):
            start = read_pos[0]
            end = start + n
            if end > len(response_data):
                raise asyncio.IncompleteReadError(response_data[start:], n)
            chunk = response_data[start:end]
            read_pos[0] = end
            return chunk

        mock_reader.readexactly = mock_readexactly

        client._reader = mock_reader
        client._writer = mock_writer

        await client.login()

        assert client._logged_in is True
        assert client._aes_key is not None
        assert client._uid == "ABCDEF1234567890"
        assert client._nonce == "TESTNONCE123"

    @pytest.mark.asyncio
    async def test_login_not_connected_raises(self):
        client = _make_client()
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_download_before_login_raises(self):
        client = _make_client()
        client._logged_in = False
        with pytest.raises(RuntimeError, match="Must login"):
            await client.download_file_replay("/test/file.mp4", "/tmp/out.h265")


class TestBaichuanStreamClientWriteFrames:
    def test_write_frames_video(self, tmp_path):
        """Test that _write_frames writes framed Annex-B video data."""
        import struct

        output_file = tmp_path / "test.h265"
        stats = {"frames_written": 0, "bytes_written": 0, "video_codec": None}

        # Simulate Annex-B NAL data
        nal_data = b"\x00\x00\x00\x01\x67\x42\x00\x1e"
        frames = [
            ("iframe", "H265", nal_data),
            ("pframe", "H265", nal_data),
            ("aac", None, b"\xff\xf1"),  # Should be skipped
            ("info", None, None),  # Should be skipped
        ]

        with open(output_file, "wb") as f:
            BaichuanStreamClient._write_frames(frames, f, stats)

        assert stats["frames_written"] == 2
        assert stats["bytes_written"] == len(nal_data) * 2
        assert stats["video_codec"] == "H265"

        # Each frame is written as: [4-byte LE timestamp][4-byte LE length][data]
        content = output_file.read_bytes()
        expected = (
            struct.pack("<I", 0)
            + struct.pack("<I", len(nal_data))
            + nal_data
            + struct.pack("<I", 0)
            + struct.pack("<I", len(nal_data))
            + nal_data
        )
        assert content == expected


# ── download_and_mux tests ───────────────────────────────────────────


class TestDownloadAndMux:
    @pytest.mark.asyncio
    async def test_download_and_mux_connection_failure(self):
        """download_and_mux returns False when connection fails."""
        from video_grouper.cameras.reolink_download import download_and_mux

        with patch(
            "video_grouper.cameras.reolink_download.BaichuanStreamClient"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
            instance.close = AsyncMock()

            result = await download_and_mux(
                host="192.168.1.200",
                port=9000,
                username="admin",
                password="pass",
                file_path="/test.mp4",
                output_mp4="/tmp/out.mp4",
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_download_and_mux_login_failure(self):
        """download_and_mux returns False when login fails."""
        from video_grouper.cameras.reolink_download import download_and_mux

        with patch(
            "video_grouper.cameras.reolink_download.BaichuanStreamClient"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock()
            instance.login = AsyncMock(
                side_effect=ConnectionError("Login failed: status=0190")
            )
            instance.close = AsyncMock()

            result = await download_and_mux(
                host="192.168.1.200",
                port=9000,
                username="admin",
                password="pass",
                file_path="/test.mp4",
                output_mp4="/tmp/out.mp4",
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_download_and_mux_no_data(self, tmp_path):
        """download_and_mux returns False when download produces no data."""
        from video_grouper.cameras.reolink_download import download_and_mux

        output = str(tmp_path / "out.mp4")

        with patch(
            "video_grouper.cameras.reolink_download.BaichuanStreamClient"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock()
            instance.login = AsyncMock()
            instance.download_file_replay = AsyncMock(
                return_value={
                    "frames_written": 0,
                    "bytes_written": 0,
                    "video_codec": None,
                    "duration_seconds": 0.0,
                }
            )
            instance.close = AsyncMock()

            result = await download_and_mux(
                host="192.168.1.200",
                port=9000,
                username="admin",
                password="pass",
                file_path="/test.mp4",
                output_mp4=output,
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_download_and_mux_closes_on_failure(self):
        """Client should be closed even on failure."""
        from video_grouper.cameras.reolink_download import download_and_mux

        with patch(
            "video_grouper.cameras.reolink_download.BaichuanStreamClient"
        ) as MockClient:
            instance = MockClient.return_value
            instance.connect = AsyncMock()
            instance.login = AsyncMock(side_effect=ConnectionError("fail"))
            instance.close = AsyncMock()

            result = await download_and_mux(
                host="192.168.1.200",
                port=9000,
                username="admin",
                password="pass",
                file_path="/test.mp4",
                output_mp4="/tmp/out.mp4",
            )
            assert result is False
            instance.close.assert_awaited_once()
