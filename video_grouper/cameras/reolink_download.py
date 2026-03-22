"""
Baichuan protocol file download for Reolink cameras.

The Reolink Duo 3 PoE's HTTP Download API is broken (firmware bug).
This module implements native Baichuan protocol download (port 9000)
to stream recordings to disk, then remux to MP4 via PyAV.
"""

import asyncio
import logging
import os
import re
import socket
import struct
import time
from hashlib import md5
from typing import Optional

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)


# ── Baichuan protocol constants ──────────────────────────────────────

HEADER_MAGIC = bytes.fromhex("f0debc0a")
AES_IV = b"0123456789abcdef"
XML_KEY = [0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF]

MSG_CLASS_MODERN = "1464"
MSG_CLASS_LEGACY = "1465"
# Legacy (1465) headers use "12dc" as the encrypt/status field, not "0000"
LEGACY_ENCRYPT_FLAG = "12dc"
HOST_CH_ID = 250


# ── BcMedia magic numbers (little-endian u32) ────────────────────────

MAGIC_INFO_V1 = 0x31303031
MAGIC_INFO_V2 = 0x32303031
MAGIC_IFRAME_START = 0x63643030
MAGIC_IFRAME_END = 0x63643039
MAGIC_PFRAME_START = 0x63643130
MAGIC_PFRAME_END = 0x63643139
MAGIC_AAC = 0x62773530
MAGIC_ADPCM = 0x62773130

PAD_SIZE = 8
IFRAME_ENCRYPT_BOUNDARY = 1024
ANNEX_B_START_CODE = b"\x00\x00\x00\x01"


# ── Crypto helpers ───────────────────────────────────────────────────


def _md5_str_modern(s: str) -> str:
    """MD5 hash: 31 uppercase hex chars (matches reolink-aio format)."""
    return md5(s.encode("utf8")).hexdigest()[:31].upper()


def _encrypt_baichuan(buf: str, offset: int) -> bytes:
    """XOR cipher for Baichuan XML payloads."""
    offset = offset % 256
    result = bytearray()
    for idx, char in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        result.append(ord(char) ^ key ^ offset)
    return bytes(result)


def _decrypt_baichuan(buf: bytes, offset: int) -> str:
    """XOR decipher for Baichuan XML payloads."""
    offset = offset % 256
    result = []
    for idx, byte_val in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        result.append(chr(byte_val ^ key ^ offset))
    return "".join(result)


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    cipher = AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
    return cipher.encrypt(data)


def _aes_decrypt(data: bytes, key: bytes) -> bytes:
    cipher = AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
    return cipher.decrypt(data)


# ── BcMedia helpers ──────────────────────────────────────────────────


def _is_known_magic(magic: int) -> bool:
    return (
        magic == MAGIC_INFO_V1
        or magic == MAGIC_INFO_V2
        or (MAGIC_IFRAME_START <= magic <= MAGIC_IFRAME_END)
        or (MAGIC_PFRAME_START <= magic <= MAGIC_PFRAME_END)
        or magic == MAGIC_AAC
        or magic == MAGIC_ADPCM
    )


def _has_start_codes(data: bytes) -> bool:
    """Check if data uses Annex-B start codes."""
    return data[:3] == b"\x00\x00\x01" or data[:4] == b"\x00\x00\x00\x01"


def _length_prefixed_to_annex_b(data: bytes) -> bytes:
    """Convert length-prefixed NAL units to Annex-B format (start codes)."""
    result = bytearray()
    offset = 0
    while offset + 4 <= len(data):
        nal_len = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        if nal_len == 0 or offset + nal_len > len(data):
            break
        result.extend(ANNEX_B_START_CODE)
        result.extend(data[offset : offset + nal_len])
        offset += nal_len
    return bytes(result)


def _to_annex_b(data: bytes) -> bytes:
    """Convert video NAL data to Annex-B, auto-detecting format."""
    if len(data) < 4:
        return data
    if _has_start_codes(data):
        return data
    return _length_prefixed_to_annex_b(data)


# ── BcMedia Demuxer ──────────────────────────────────────────────────


class BcMediaDemuxer:
    """Parse BcMedia binary stream and yield video/audio frames.

    Feed raw (decrypted) bytes via feed(). Returns list of
    (frame_type, codec, data) tuples per call.
    frame_type: "info", "iframe", "pframe", "aac", "adpcm"
    """

    def __init__(self):
        self._buffer = bytearray()
        self.video_codec: Optional[str] = None
        self.width: int = 0
        self.height: int = 0
        self.fps: int = 0
        self.last_microseconds: int = 0

    def feed(self, data: bytes) -> list:
        """Feed data and return list of parsed frames."""
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= 4:
            magic = struct.unpack_from("<I", self._buffer, 0)[0]
            if not _is_known_magic(magic):
                # Try to resync by finding the next valid magic
                found = False
                for i in range(1, len(self._buffer) - 3):
                    m = struct.unpack_from("<I", self._buffer, i)[0]
                    if _is_known_magic(m):
                        self._buffer = self._buffer[i:]
                        found = True
                        break
                if not found:
                    if len(self._buffer) > 3:
                        self._buffer = self._buffer[-3:]
                    break
                continue

            result = self._try_parse(magic)
            if result is None:
                break  # Incomplete packet, wait for more data
            frame, consumed = result
            frames.append(frame)
            self._buffer = self._buffer[consumed:]

        return frames

    def _try_parse(self, magic: int):
        buf = self._buffer
        if magic in (MAGIC_INFO_V1, MAGIC_INFO_V2):
            return self._parse_info(buf)
        elif MAGIC_IFRAME_START <= magic <= MAGIC_IFRAME_END:
            return self._parse_video_frame(buf, "iframe")
        elif MAGIC_PFRAME_START <= magic <= MAGIC_PFRAME_END:
            return self._parse_video_frame(buf, "pframe")
        elif magic == MAGIC_AAC:
            return self._parse_audio(buf)
        elif magic == MAGIC_ADPCM:
            return self._parse_adpcm(buf)
        return None

    def _parse_info(self, buf):
        if len(buf) < 32:
            return None
        header_size = struct.unpack_from("<I", buf, 4)[0]
        if header_size != 32:
            return None
        self.width = struct.unpack_from("<I", buf, 8)[0]
        self.height = struct.unpack_from("<I", buf, 12)[0]
        self.fps = buf[17]
        return (("info", None, None), 32)

    def _parse_video_frame(self, buf, frame_type):
        if len(buf) < 24:
            return None
        codec_str = bytes(buf[4:8]).decode("ascii", errors="ignore")
        if codec_str not in ("H264", "H265"):
            return None
        self.video_codec = codec_str
        payload_size = struct.unpack_from("<I", buf, 8)[0]
        additional_header_size = struct.unpack_from("<I", buf, 12)[0]
        microseconds = struct.unpack_from("<I", buf, 16)[0]
        # Store latest timestamp for callers
        self.last_microseconds = microseconds
        offset = 24
        if len(buf) < offset + additional_header_size:
            return None
        offset += additional_header_size
        if len(buf) < offset + payload_size:
            return None
        data = bytes(buf[offset : offset + payload_size])
        offset += payload_size
        pad = (
            0 if payload_size % PAD_SIZE == 0 else PAD_SIZE - (payload_size % PAD_SIZE)
        )
        if len(buf) < offset + pad:
            return None
        offset += pad
        return ((frame_type, codec_str, data), offset)

    def _parse_audio(self, buf):
        if len(buf) < 8:
            return None
        size_a = struct.unpack_from("<H", buf, 4)[0]
        size_b = struct.unpack_from("<H", buf, 6)[0]
        if size_a != size_b:
            return None
        pad = 0 if size_a % PAD_SIZE == 0 else PAD_SIZE - (size_a % PAD_SIZE)
        total = 8 + size_a + pad
        if len(buf) < total:
            return None
        data = bytes(buf[8 : 8 + size_a])
        return (("aac", None, data), total)

    def _parse_adpcm(self, buf):
        if len(buf) < 12:
            return None
        size_a = struct.unpack_from("<H", buf, 4)[0]
        size_b = struct.unpack_from("<H", buf, 6)[0]
        if size_a != size_b:
            return None
        sub_header = 4
        if size_a < sub_header:
            return None
        block_size = size_a - sub_header
        pad = 0 if size_a % PAD_SIZE == 0 else PAD_SIZE - (size_a % PAD_SIZE)
        total = 12 + block_size + pad
        if len(buf) < total:
            return None
        data = bytes(buf[12 : 12 + block_size])
        return (("adpcm", None, data), total)


# ── Baichuan Stream Client ───────────────────────────────────────────


class BaichuanStreamClient:
    """Low-level Baichuan TCP client for streaming file downloads.

    Handles login and binary file download via cmd_id=5 (replay).
    Manages its own TCP connection for streaming without memory buffering.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._aes_key: Optional[bytes] = None
        self._nonce: Optional[str] = None
        self._uid: Optional[str] = None
        self._mess_id = 0
        self._session_id = (
            20  # Session counter for replay channelId (camera rejects low values)
        )
        self._logged_in = False

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 30.0):
        """Open TCP connection to camera on Baichuan port.

        Args:
            timeout: Connection timeout in seconds (default 30s).
        """
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=timeout,
        )
        # Tune TCP socket for download throughput (2x speed improvement)
        sock = self._writer.transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    async def close(self):
        """Close TCP connection."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None
        self._logged_in = False

    # ── Header construction ──────────────────────────────────────────
    #
    # Baichuan header format (from nodelink-js framing.ts):
    #   Bytes  0-3:  magic (f0debc0a)
    #   Bytes  4-7:  cmdId (u32 LE)
    #   Bytes  8-11: bodyLen (u32 LE)
    #   Byte   12:   channelId (u8)
    #   Byte   13:   streamType (u8)
    #   Bytes 14-15: msgNum (u16 LE)
    #   Bytes 16-17: responseCode (u16 LE)
    #   Bytes 18-19: messageClass (u16 LE)
    #   Bytes 20-23: payloadOffset (u32 LE) - only for 24-byte headers

    def _next_msg_num(self) -> int:
        self._mess_id = (self._mess_id + 1) % 65536
        return self._mess_id

    def _build_header(
        self,
        cmd_id: int,
        body_length: int,
        channel_id: int = HOST_CH_ID,
        stream_type: int = 0,
        msg_num: int | None = None,
        response_code: str = "0000",
        message_class: str = "1464",
        payload_offset: int = 0,
    ) -> bytes:
        """Build a 24-byte modern Baichuan message header.

        Note: responseCode and messageClass are 2-byte hex strings (NOT LE u16)
        matching reolink-aio's bytes.fromhex() encoding.
        """
        if msg_num is None:
            msg_num = self._next_msg_num()
        has_offset = message_class in ("1464", "0000", "6482")
        hdr = (
            HEADER_MAGIC
            + struct.pack("<I", cmd_id)
            + struct.pack("<I", body_length)
            + struct.pack("<B", channel_id & 0xFF)
            + struct.pack("<B", stream_type & 0xFF)
            + struct.pack("<H", msg_num & 0xFFFF)
            + bytes.fromhex(response_code + message_class)
        )
        if has_offset:
            hdr += struct.pack("<I", payload_offset)
        return hdr

    def _build_header_legacy(
        self,
        cmd_id: int,
        body_length: int,
        channel_id: int = HOST_CH_ID,
    ) -> bytes:
        """Build a 20-byte legacy Baichuan header (message_class=1465).

        Legacy headers use "12dc" as the encrypt indicator.
        """
        msg_num = self._next_msg_num()
        return (
            HEADER_MAGIC
            + struct.pack("<I", cmd_id)
            + struct.pack("<I", body_length)
            + struct.pack("<B", channel_id & 0xFF)
            + struct.pack("<B", 0)  # streamType=0
            + struct.pack("<H", msg_num & 0xFFFF)
            + bytes.fromhex("12dc" + "1465")
        )

    # ── Message I/O ──────────────────────────────────────────────────

    @staticmethod
    def _has_payload_offset(message_class_hex: str) -> bool:
        """Check if this message class uses a 24-byte header with payload_offset."""
        return message_class_hex in ("1464", "0000", "6482")

    async def _read_header(self) -> dict:
        """Read and parse a Baichuan message header (20 or 24 bytes).

        Bytes 16-19 (responseCode + messageClass) are read as raw hex strings
        matching reolink-aio's convention. responseCode is also parsed as a
        little-endian u16 integer for numeric comparisons.
        """
        data = await self._reader.readexactly(20)
        if data[0:4] != HEADER_MAGIC:
            raise ConnectionError(f"Invalid Baichuan magic: {data[0:4].hex()}")

        cmd_id = struct.unpack_from("<I", data, 4)[0]
        body_length = struct.unpack_from("<I", data, 8)[0]
        channel_id = data[12]
        stream_type = data[13]
        msg_num = struct.unpack_from("<H", data, 14)[0]
        # responseCode as LE u16 for numeric comparison (200=OK, 400=error)
        response_code = struct.unpack_from("<H", data, 16)[0]
        # messageClass as hex string for payload_offset detection
        message_class = data[18:20].hex()

        payload_offset = 0
        len_header = 20

        if self._has_payload_offset(message_class):
            extra = await self._reader.readexactly(4)
            payload_offset = struct.unpack("<I", extra)[0]
            len_header = 24

        return {
            "cmd_id": cmd_id,
            "body_length": body_length,
            "channel_id": channel_id,
            "stream_type": stream_type,
            "msg_num": msg_num,
            "response_code": response_code,
            "message_class": message_class,
            "payload_offset": payload_offset,
            "len_header": len_header,
        }

    async def _read_message(self) -> tuple:
        """Read a full Baichuan message. Returns (header, xml_body, payload)."""
        hdr = await self._read_header()
        body_length = hdr["body_length"]

        full_body = b""
        if body_length > 0:
            full_body = await self._reader.readexactly(body_length)

        payload_offset = hdr["payload_offset"]
        if payload_offset > 0 and payload_offset <= len(full_body):
            xml_body = full_body[:payload_offset]
            payload = full_body[payload_offset:]
        else:
            xml_body = full_body
            payload = b""

        return hdr, xml_body, payload

    # ── Login flow ───────────────────────────────────────────────────

    async def login(self):
        """Perform Baichuan login: nonce exchange + credential auth."""
        # Step 1: Request nonce (cmd_id=1, legacy 20-byte header, empty body)
        header = self._build_header_legacy(cmd_id=1, body_length=0)
        self._writer.write(header)
        await self._writer.drain()

        hdr, body, _ = await self._read_message()
        if hdr["cmd_id"] != 1:
            raise ConnectionError(
                f"Expected cmd_id=1 nonce response, got {hdr['cmd_id']}"
            )

        body_str = _decrypt_baichuan(body, hdr["channel_id"])
        nonce_match = re.search(r"<nonce>([^<]+)</nonce>", body_str)
        if not nonce_match:
            raise ConnectionError(f"No nonce in login response: {body_str[:200]}")
        self._nonce = nonce_match.group(1)

        # Derive AES key: MD5(nonce-password) first 16 chars, UTF-8 encoded
        aes_key_str = _md5_str_modern(f"{self._nonce}-{self._password}")[:16]
        self._aes_key = aes_key_str.encode("utf8")

        # Step 2: Send login XML with hashed credentials (Baichuan XOR, not AES)
        # Login uses MODERN header (0x1464) per reolink-aio protocol
        user_hash = _md5_str_modern(f"{self._username}{self._nonce}")
        password_hash = _md5_str_modern(f"{self._password}{self._nonce}")

        login_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            "<body>"
            '<LoginUser version="1.1">'
            f"<userName>{user_hash}</userName>"
            f"<password>{password_hash}</password>"
            "<userVer>1</userVer>"
            "</LoginUser>"
            '<LoginNet version="1.1">'
            "<type>LAN</type>"
            "<udpPort>0</udpPort>"
            "</LoginNet>"
            "</body>"
        )

        encrypted = _encrypt_baichuan(login_xml, HOST_CH_ID)
        header = self._build_header(
            cmd_id=1,
            body_length=len(encrypted),
            channel_id=HOST_CH_ID,
            payload_offset=0,
        )
        self._writer.write(header + encrypted)
        await self._writer.drain()

        hdr, body, _ = await self._read_message()
        # responseCode 200 = success
        if hdr["response_code"] not in (200, 0):
            raise ConnectionError(f"Login failed: responseCode={hdr['response_code']}")

        # Parse UID from login response
        body_str = _decrypt_baichuan(body, hdr["channel_id"])
        uid_match = re.search(r"<uid>([^<]+)</uid>", body_str)
        if uid_match:
            self._uid = uid_match.group(1)

        self._logged_in = True
        logger.info(f"Baichuan login successful (uid={self._uid})")

    async def _discover_uid(self) -> str | None:
        """Get camera UID, discovering via cmd_id=114 (GetP2p) if needed.

        Returns None if UID cannot be discovered (OK for standalone cameras).
        """
        if self._uid:
            return self._uid

        try:
            body_xml = (
                '<?xml version="1.0" encoding="UTF-8" ?>'
                "<body>"
                '<P2p version="1.1"></P2p>'
                "</body>"
            )
            encrypted = _aes_encrypt(body_xml.encode("utf8"), self._aes_key)
            header = self._build_header(cmd_id=114, body_length=len(encrypted))
            self._writer.write(header + encrypted)
            await self._writer.drain()

            hdr, body, _ = await self._read_message()
            if body:
                decrypted = _aes_decrypt(body, self._aes_key).decode(
                    "utf8", errors="ignore"
                )
                uid_match = re.search(r"<uid>([^<]+)</uid>", decrypted)
                if uid_match:
                    self._uid = uid_match.group(1)
        except Exception:
            logger.debug("UID discovery failed (not required for standalone cameras)")

        return self._uid

    # ── Stream chunk decryption ──────────────────────────────────────

    @staticmethod
    def _score_bcmedia(data: bytes, max_scan: int = 65536) -> int:
        """Score how BcMedia-like a buffer is by scanning for magic bytes.

        Returns count*1000 - first_offset (higher = more BcMedia-like).
        Matches nodelink-js scoreBcMediaLike heuristic.
        """
        if len(data) < 4:
            return -1
        limit = min(max_scan, len(data) - 3)
        count = 0
        first = -1
        for i in range(limit):
            m = struct.unpack_from("<I", data, i)[0]
            if _is_known_magic(m):
                count += 1
                if first < 0:
                    first = i
                if count > 32 and first == 0:
                    break
        return count * 1000 - (50000 if first < 0 else first)

    def _decrypt_stream_chunk(
        self, payload: bytes, encrypt_len: int | None = None
    ) -> bytes:
        """Decrypt a BcMedia stream chunk with partial encryption handling.

        Reolink cameras use partial AES encryption:
        - encryptLen from extension XML: only first N bytes encrypted
        - I-frames >1024 bytes: only first 1024 bytes encrypted
        - P-frames: header encrypted, NAL payload cleartext
        - Small frames: fully encrypted
        - Some cameras send unencrypted data (stream encryption disabled)

        """
        if not self._aes_key or len(payload) == 0:
            return payload

        # If encryptLen is specified, decrypt only that many bytes.
        # This MUST be checked first -- even "unencrypted" streams use
        # partial encryption (first 1024 bytes) for video frame payloads.
        if encrypt_len is not None and 0 < encrypt_len < len(payload):
            dec_part = _aes_decrypt(payload[:encrypt_len], self._aes_key)
            return dec_part + payload[encrypt_len:]

        # Check if raw data already has valid BcMedia magic (not encrypted)
        if len(payload) >= 4:
            raw_magic = struct.unpack_from("<I", payload, 0)[0]
            if _is_known_magic(raw_magic):
                return payload

        # Try fresh AES decryption and check for BcMedia magic
        fresh = _aes_decrypt(payload, self._aes_key)
        if len(fresh) >= 4:
            magic = struct.unpack_from("<I", fresh, 0)[0]
            if _is_known_magic(magic):
                # I-frame partial encryption: only first 1024 bytes encrypted
                if len(payload) > IFRAME_ENCRYPT_BOUNDARY:
                    dec_head = _aes_decrypt(
                        payload[:IFRAME_ENCRYPT_BOUNDARY], self._aes_key
                    )
                    return dec_head + payload[IFRAME_ENCRYPT_BOUNDARY:]
                return fresh

        # P-frame partial encryption: header encrypted, NAL start codes in raw tail
        if len(payload) > 28:
            for header_len in (24, 28, 32):
                if header_len + 4 <= len(payload):
                    tail = payload[header_len:]
                    if tail[:3] == b"\x00\x00\x01" or (tail[:4] == b"\x00\x00\x00\x01"):
                        dec_hdr = _aes_decrypt(payload[:header_len], self._aes_key)
                        return dec_hdr + tail

        # Fallback: compare raw vs decrypted using BcMedia magic scan.
        raw_score = self._score_bcmedia(payload)
        dec_score = self._score_bcmedia(fresh)
        if raw_score >= dec_score:
            return payload
        return fresh

    # ── File download (cmd_id=5 replay) ─────────────────────────────

    async def download_file_replay(
        self,
        file_path: str,
        output_path: str,
        channel: int = 0,
        on_progress=None,
    ) -> dict:
        """Download a recording via Baichuan replay, writing raw video to disk.

        Sends cmd_id=5 (FileInfoList replay) and streams the BcMedia response
        to a raw video file (H.265 or H.264 Annex-B bitstream).

        Protocol flow (from nodelink-js PCAP analysis):
        1. Send cmd_id=5 with replay XML (channelId=session_counter, msgNum=0)
        2. Camera sends initial ack (Extension XML with binaryData=1)
        3. Camera sends binary data as push frames (same cmd_id=5)
        4. Idle timeout (15s no data) indicates end of stream

        Args:
            file_path: Camera file path (e.g. "/mnt/sda/Mp4Record/...")
            output_path: Path for raw video output file (.h265 or .h264)
            channel: Camera channel (default 0)
            on_progress: Optional callback(bytes_written, elapsed_seconds)

        Returns:
            Dict with download stats.
        """
        if not self._logged_in:
            raise RuntimeError("Must login before downloading")

        # Use cached UID if available; skip discovery for replay since
        # a failed cmd_id=114 poisons the TCP session on some firmware.
        uid = self._uid

        # Build replay request XML (cmd_id=5)
        # PCAP: xmlChannelId=0 works, UID omitted for standalone cameras
        uid_element = f"<uid>{uid}</uid>" if uid else ""
        replay_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>\n'
            "<body>\n"
            '<FileInfoList version="1.1">\n'
            "<FileInfo>\n"
            f"<channelId>{channel}</channelId>\n"
            f"<Id>{file_path}</Id>\n"
            f"{uid_element}"
            "<supportSub>0</supportSub>\n"
            "<playSpeed>1</playSpeed>\n"
            "<streamType>mainStream</streamType>\n"
            "</FileInfo>\n"
            "</FileInfoList>\n"
            "</body>"
        )

        # PCAP: request uses NO separate extension (payloadOffset=0)
        # Body is just the AES-encrypted replay XML
        enc_body = _aes_encrypt(replay_xml.encode("utf8"), self._aes_key)

        # PCAP: channelId = session counter (not channel+1, not 250)
        # PCAP: msgNum = 0 always for FileInfoList replay
        session_id = self._next_msg_num()
        header = self._build_header(
            cmd_id=5,
            body_length=len(enc_body),
            channel_id=session_id,
            stream_type=0,
            msg_num=0,
            response_code="0000",
            message_class="1464",
            payload_offset=0,
        )
        self._writer.write(header + enc_body)
        await self._writer.drain()

        # Stream BcMedia data to disk
        # Binary data arrives as push frames with cmd_id=5 after the initial ack
        demuxer = BcMediaDemuxer()
        stats = {
            "frames_written": 0,
            "bytes_written": 0,
            "video_codec": None,
            "duration_seconds": 0.0,
        }
        start_time = time.monotonic()
        last_progress = start_time
        # Idle timeout: cameras may pause between GOP boundaries
        idle_timeout = 15

        # Write framed video (each frame: 4-byte timestamp + 4-byte length + data)
        with open(output_path, "wb") as f:
            while True:
                try:
                    hdr, xml_body, payload = await asyncio.wait_for(
                        self._read_message(), timeout=idle_timeout
                    )
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    if stats["bytes_written"] > 0:
                        logger.info("Download stream ended (idle timeout)")
                    else:
                        logger.warning("No data received before timeout")
                    break

                # Only process frames for our cmd_id=5 replay
                if hdr["cmd_id"] != 5:
                    continue

                # Check for hard errors (4xx but not 60xxx special codes)
                rc = hdr["response_code"]
                if 400 <= rc < 60000:
                    if stats["bytes_written"] > 0:
                        logger.info(f"Download ended (responseCode={rc})")
                        break
                    raise ConnectionError(
                        f"Replay rejected: responseCode={rc}, "
                        f"channelId={hdr['channel_id']}"
                    )

                # Check extension XML for encryptLen.
                # Only use encryptLen when explicitly present -- do NOT default.
                # When absent, _decrypt_stream_chunk compares raw vs decrypted
                # BcMedia scores (matching nodelink-js decryptBinaryForReplay).
                encrypt_len = None
                if xml_body:
                    try:
                        ext_dec = _aes_decrypt(xml_body, self._aes_key).decode(
                            "utf8", errors="ignore"
                        )
                        m = re.search(
                            r"<encryptLen>(\d+)</encryptLen>", ext_dec, re.IGNORECASE
                        )
                        if m:
                            encrypt_len = int(m.group(1))
                    except Exception:
                        pass

                if not payload:
                    continue

                # Decrypt binary payload
                dec = self._decrypt_stream_chunk(payload, encrypt_len)
                frames = demuxer.feed(dec)
                self._write_frames(frames, f, stats, demuxer)

                # Progress callback
                now = time.monotonic()
                if on_progress and now - last_progress >= 5.0:
                    on_progress(stats["bytes_written"], now - start_time)
                    last_progress = now

        stats["duration_seconds"] = time.monotonic() - start_time
        return stats

    @staticmethod
    def _write_frames(frames: list, f, stats: dict, demuxer=None):
        """Write parsed video frames to file as Annex-B NAL units.

        Each frame is written as (4-byte LE microsecond timestamp + Annex-B data)
        so the remuxer can assign real PTS without a sidecar file.
        """
        for frame_type, codec, data in frames:
            if frame_type in ("iframe", "pframe") and data:
                annexb = _to_annex_b(data)
                # Write timestamp prefix (4 bytes LE) + frame data
                us = demuxer.last_microseconds if demuxer else 0
                f.write(struct.pack("<I", us))
                f.write(struct.pack("<I", len(annexb)))
                f.write(annexb)
                stats["frames_written"] += 1
                stats["bytes_written"] += len(annexb)
                if codec:
                    stats["video_codec"] = codec


# ── High-level download + mux ────────────────────────────────────────


def _detect_hevc(raw_path: str) -> bool:
    """Check if a raw bitstream is HEVC by inspecting NAL unit types."""
    with open(raw_path, "rb") as f:
        header = f.read(64)
    # Look for Annex-B start code followed by HEVC NAL types
    for i in range(len(header) - 5):
        if header[i : i + 4] == b"\x00\x00\x00\x01":
            nal_type = (header[i + 4] >> 1) & 0x3F
            # HEVC NAL types 32-34 are VPS/SPS/PPS (H.264 doesn't have these)
            if nal_type in (32, 33, 34):
                return True
    return False


def _remux_raw_to_mp4(raw_path: str, mp4_path: str, codec: str = "H265"):
    """Remux framed video data to MP4 container via PyAV.

    The raw file contains frames in our custom format:
      [4-byte LE microsecond timestamp][4-byte LE data length][Annex-B NAL data]

    We reassemble the Annex-B stream into a temp file for PyAV to parse,
    using the embedded microsecond timestamps for accurate PTS.
    """
    import av

    # Read all frames with their timestamps
    frames = []
    with open(raw_path, "rb") as f:
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            us = struct.unpack_from("<I", hdr, 0)[0]
            data_len = struct.unpack_from("<I", hdr, 4)[0]
            data = f.read(data_len)
            if len(data) < data_len:
                break
            frames.append((us, data))

    if not frames:
        return

    # Write a clean Annex-B file for PyAV to parse
    annexb_path = raw_path + ".annexb"
    with open(annexb_path, "wb") as f:
        for _, data in frames:
            f.write(data)

    try:
        # Detect codec from actual NAL data (camera may report wrong codec)
        if _detect_hevc(annexb_path):
            fmt = "hevc"
        else:
            fmt = "hevc" if codec == "H265" else "h264"

        with av.open(annexb_path, format=fmt) as input_ct:
            with av.open(mp4_path, "w", options={"movflags": "faststart"}) as output_ct:
                in_stream = input_ct.streams.video[0]
                out_stream = output_ct.add_stream_from_template(in_stream)
                tb = out_stream.time_base or in_stream.time_base

                # Normalize timestamps: start at 0, handle u32 wraparound
                base_us = frames[0][0]
                frame_idx = 0

                for packet in input_ct.demux(in_stream):
                    if packet.size == 0:
                        continue
                    if frame_idx < len(frames):
                        us = frames[frame_idx][0]
                        delta_us = (us - base_us) & 0xFFFFFFFF
                        pts = int(delta_us * 1e-6 / float(tb))
                    else:
                        # Extrapolate if we run out of timestamps
                        pts = int(frame_idx / 20.0 / float(tb))
                    packet.dts = pts
                    packet.pts = pts
                    frame_idx += 1
                    packet.stream = out_stream
                    output_ct.mux(packet)
    finally:
        if os.path.exists(annexb_path):
            os.remove(annexb_path)


def _download_and_mux_sync(
    host: str,
    port: int,
    username: str,
    password: str,
    file_path: str,
    output_mp4: str,
    channel: int = 0,
    on_progress=None,
) -> bool:
    """Run download + mux in a dedicated event loop (called from a thread).

    Having a private event loop prevents contention with the main service
    loop (camera poller, PlayMetrics, NTFY, etc.) which was halving
    download throughput.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _download_and_mux_async(
                host,
                port,
                username,
                password,
                file_path,
                output_mp4,
                channel,
                on_progress,
            )
        )
    finally:
        loop.close()


async def _download_and_mux_async(
    host: str,
    port: int,
    username: str,
    password: str,
    file_path: str,
    output_mp4: str,
    channel: int = 0,
    on_progress=None,
) -> bool:
    """Download a recording via Baichuan protocol and mux to MP4.

    1. Connect and login via Baichuan (port 9000)
    2. Stream download to temp raw video file (BcMedia -> Annex-B)
    3. Remux to MP4 via ffmpeg (stream copy, no re-encoding)
    4. Clean up temp file

    Returns True on success.
    """
    temp_raw = output_mp4 + ".raw.tmp"
    client = BaichuanStreamClient(host, port, username, password)

    try:
        await client.connect()  # has built-in 30s timeout
        await asyncio.wait_for(client.login(), timeout=30.0)

        stats = await client.download_file_replay(
            file_path=file_path,
            output_path=temp_raw,
            channel=channel,
            on_progress=on_progress,
        )

        if stats["bytes_written"] == 0:
            logger.error("Baichuan download produced no video data")
            return False

        logger.info(
            f"Baichuan download complete: {stats['frames_written']} frames, "
            f"{stats['bytes_written'] / 1024 / 1024:.1f}MB in "
            f"{stats['duration_seconds']:.1f}s"
        )

        # Remux raw bitstream -> MP4
        codec = stats.get("video_codec") or "H265"
        _remux_raw_to_mp4(temp_raw, output_mp4, codec)
        logger.info(f"Remuxed to {os.path.basename(output_mp4)}")

        return True

    except Exception as e:
        logger.error(f"Baichuan download failed: {e}", exc_info=True)
        return False
    finally:
        await client.close()
        if os.path.exists(temp_raw):
            try:
                os.remove(temp_raw)
            except OSError:
                pass


async def download_and_mux(
    host: str,
    port: int,
    username: str,
    password: str,
    file_path: str,
    output_mp4: str,
    channel: int = 0,
    on_progress=None,
) -> bool:
    """Download a recording via Baichuan protocol and mux to MP4.

    Runs in a dedicated thread with its own event loop to avoid contention
    with the main service loop.
    """
    return await asyncio.to_thread(
        _download_and_mux_sync,
        host,
        port,
        username,
        password,
        file_path,
        output_mp4,
        channel,
        on_progress,
    )
