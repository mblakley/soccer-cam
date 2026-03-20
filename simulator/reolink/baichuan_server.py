"""Baichuan TCP protocol server for Reolink simulator.

Server-side implementation of the binary protocol consumed by
video_grouper/cameras/reolink_download.py (BaichuanStreamClient).

Protocol flow:
1. Client sends cmd_id=1 (legacy 20-byte header) requesting nonce
2. Server responds with XOR-encrypted XML containing random nonce
3. Client sends cmd_id=1 (modern 24-byte header) with hashed credentials
4. Server validates and responds with login success + UID
5. Client sends cmd_id=5 (AES-encrypted replay XML) for file download
6. Server streams BcMedia-encoded video as push frames
"""

import asyncio
import logging
import secrets
import struct
from typing import Optional

from .bcmedia_encoder import encode_file_to_bcmedia
from .crypto import (
    aes_decrypt,
    decrypt_baichuan,
    encrypt_baichuan,
    md5_str_modern,
)

logger = logging.getLogger(__name__)

HEADER_MAGIC = bytes.fromhex("f0debc0a")
HOST_CH_ID = 250

# Message classes
MSG_CLASS_MODERN = "1464"
MSG_CLASS_LEGACY = "1465"


def _has_payload_offset(message_class: str) -> bool:
    return message_class in ("1464", "0000", "6482")


class BaichuanServer:
    """Async TCP server implementing Baichuan protocol for Reolink cameras."""

    def __init__(
        self, storage, username: str, password: str, activity_log: Optional[list] = None
    ):
        self._storage = storage
        self._username = username
        self._password = password
        self._activity_log = activity_log
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_connections = 0

    def _log(self, msg):
        if self._activity_log is not None:
            self._activity_log.append(f"[BC] {msg}")

    @property
    def active_connections(self) -> int:
        return self._active_connections

    async def start(self, host: str = "0.0.0.0", port: int = 9000):
        self._server = await asyncio.start_server(self._handle_client, host, port)
        logger.info(f"Baichuan server listening on {host}:{port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ── Header parsing ────────────────────────────────────────────────

    @staticmethod
    async def _read_header(reader: asyncio.StreamReader) -> dict:
        """Read and parse a Baichuan message header (20 or 24 bytes)."""
        data = await reader.readexactly(20)
        if data[0:4] != HEADER_MAGIC:
            raise ConnectionError(f"Invalid magic: {data[0:4].hex()}")

        cmd_id = struct.unpack_from("<I", data, 4)[0]
        body_length = struct.unpack_from("<I", data, 8)[0]
        channel_id = data[12]
        stream_type = data[13]
        msg_num = struct.unpack_from("<H", data, 14)[0]
        response_code = struct.unpack_from("<H", data, 16)[0]
        message_class = data[18:20].hex()

        payload_offset = 0
        if _has_payload_offset(message_class):
            extra = await reader.readexactly(4)
            payload_offset = struct.unpack("<I", extra)[0]

        return {
            "cmd_id": cmd_id,
            "body_length": body_length,
            "channel_id": channel_id,
            "stream_type": stream_type,
            "msg_num": msg_num,
            "response_code": response_code,
            "message_class": message_class,
            "payload_offset": payload_offset,
        }

    @staticmethod
    async def _read_body(
        reader: asyncio.StreamReader, header: dict
    ) -> tuple[bytes, bytes]:
        """Read message body, split into xml_body and payload by payload_offset."""
        body_length = header["body_length"]
        if body_length == 0:
            return b"", b""

        full_body = await reader.readexactly(body_length)
        offset = header["payload_offset"]
        if 0 < offset <= len(full_body):
            return full_body[:offset], full_body[offset:]
        return full_body, b""

    # ── Header construction ───────────────────────────────────────────

    @staticmethod
    def _build_header(
        cmd_id: int,
        body_length: int,
        channel_id: int = HOST_CH_ID,
        msg_num: int = 0,
        response_code: int = 200,
        message_class: str = MSG_CLASS_MODERN,
        payload_offset: int = 0,
    ) -> bytes:
        """Build a Baichuan message header."""
        # responseCode as LE u16, then messageClass as raw hex bytes
        rc_bytes = struct.pack("<H", response_code & 0xFFFF)
        mc_bytes = bytes.fromhex(message_class)

        hdr = (
            HEADER_MAGIC
            + struct.pack("<I", cmd_id)
            + struct.pack("<I", body_length)
            + struct.pack("<B", channel_id & 0xFF)
            + struct.pack("<B", 0)  # stream_type
            + struct.pack("<H", msg_num & 0xFFFF)
            + rc_bytes
            + mc_bytes
        )

        if _has_payload_offset(message_class):
            hdr += struct.pack("<I", payload_offset)

        return hdr

    @staticmethod
    def _build_header_legacy(
        cmd_id: int,
        body_length: int,
        channel_id: int = HOST_CH_ID,
    ) -> bytes:
        """Build a 20-byte legacy Baichuan header (message_class=1465)."""
        return (
            HEADER_MAGIC
            + struct.pack("<I", cmd_id)
            + struct.pack("<I", body_length)
            + struct.pack("<B", channel_id & 0xFF)
            + struct.pack("<B", 0)
            + struct.pack("<H", 0)
            + bytes.fromhex("12dc" + "1465")
        )

    # ── Client handler ────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        addr = writer.get_extra_info("peername")
        self._active_connections += 1
        self._log(f"Connection from {addr}")
        logger.info(f"Baichuan client connected: {addr}")

        aes_key: Optional[bytes] = None
        nonce: Optional[str] = None

        try:
            while True:
                header = await self._read_header(reader)
                xml_body, payload = await self._read_body(reader, header)

                cmd_id = header["cmd_id"]

                if cmd_id == 1:
                    aes_key, nonce = await self._handle_login(
                        writer, header, xml_body, nonce
                    )

                elif cmd_id == 5:
                    await self._handle_replay(writer, header, xml_body, aes_key)

                elif cmd_id == 114:
                    await self._handle_get_p2p(writer, header, aes_key)

                else:
                    logger.debug(f"Ignoring cmd_id={cmd_id}")

        except (asyncio.IncompleteReadError, ConnectionError, ConnectionResetError):
            logger.info(f"Baichuan client disconnected: {addr}")
        except Exception as e:
            logger.error(f"Baichuan client error: {e}", exc_info=True)
        finally:
            self._active_connections -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # ── Command handlers ──────────────────────────────────────────────

    async def _handle_login(
        self,
        writer: asyncio.StreamWriter,
        header: dict,
        xml_body: bytes,
        existing_nonce: Optional[str],
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Handle cmd_id=1: nonce exchange or credential login.

        Returns (aes_key, nonce) tuple.
        """
        message_class = header["message_class"]

        if message_class == "1465" or (message_class != "1464" and len(xml_body) == 0):
            # Step 1: Nonce request (legacy header, empty body)
            nonce = secrets.token_hex(16)
            nonce_xml = (
                '<?xml version="1.0" encoding="UTF-8" ?>'
                "<body>"
                '<LoginUser version="1.1">'
                f"<nonce>{nonce}</nonce>"
                "</LoginUser>"
                "</body>"
            )

            encrypted = encrypt_baichuan(nonce_xml, header["channel_id"])
            resp_header = self._build_header_legacy(
                cmd_id=1,
                body_length=len(encrypted),
                channel_id=header["channel_id"],
            )
            writer.write(resp_header + encrypted)
            await writer.drain()

            self._log(f"Nonce sent: {nonce[:8]}...")
            logger.debug(f"Sent nonce to client (nonce={nonce[:16]}...)")
            return None, nonce

        else:
            # Step 2: Credential validation (modern header, XOR-encrypted XML)
            nonce = existing_nonce
            if not nonce:
                logger.warning("Login attempt without prior nonce exchange")
                resp_header = self._build_header(
                    cmd_id=1,
                    body_length=0,
                    response_code=400,
                )
                writer.write(resp_header)
                await writer.drain()
                return None, None

            # Decrypt login XML
            body_str = decrypt_baichuan(xml_body, header["channel_id"])

            # Validate credentials
            expected_user = md5_str_modern(f"{self._username}{nonce}")
            expected_pass = md5_str_modern(f"{self._password}{nonce}")

            import re

            user_match = re.search(r"<userName>([^<]+)</userName>", body_str)
            pass_match = re.search(r"<password>([^<]+)</password>", body_str)

            if not user_match or not pass_match:
                logger.warning("Login XML missing credentials")
                resp_header = self._build_header(
                    cmd_id=1, body_length=0, response_code=400
                )
                writer.write(resp_header)
                await writer.drain()
                return None, nonce

            if (
                user_match.group(1) != expected_user
                or pass_match.group(1) != expected_pass
            ):
                self._log("Login FAILED (bad credentials)")
                logger.warning("Baichuan login failed: credential mismatch")
                resp_header = self._build_header(
                    cmd_id=1, body_length=0, response_code=400
                )
                writer.write(resp_header)
                await writer.drain()
                return None, nonce

            # Success: derive AES key and send response
            aes_key_str = md5_str_modern(f"{nonce}-{self._password}")[:16]
            aes_key = aes_key_str.encode("utf8")

            uid = "SIM" + secrets.token_hex(8).upper()
            response_xml = (
                '<?xml version="1.0" encoding="UTF-8" ?>'
                "<body>"
                '<LoginUser version="1.1">'
                f"<uid>{uid}</uid>"
                "</LoginUser>"
                "</body>"
            )

            encrypted = encrypt_baichuan(response_xml, header["channel_id"])
            resp_header = self._build_header(
                cmd_id=1,
                body_length=len(encrypted),
                channel_id=header["channel_id"],
                response_code=200,
            )
            writer.write(resp_header + encrypted)
            await writer.drain()

            self._log(f"Login OK (uid={uid[:8]}...)")
            logger.info(f"Baichuan login successful: uid={uid}")
            return aes_key, nonce

    async def _handle_replay(
        self,
        writer: asyncio.StreamWriter,
        header: dict,
        xml_body: bytes,
        aes_key: Optional[bytes],
    ):
        """Handle cmd_id=5: file replay/download.

        1. Decrypt request XML to get file path
        2. Send initial ack (Extension XML with binaryData=1)
        3. Stream BcMedia-encoded video frames
        """
        if not aes_key:
            logger.warning("Replay request without login")
            return

        # Decrypt the replay request XML
        try:
            decrypted = aes_decrypt(xml_body, aes_key).decode("utf8", errors="ignore")
        except Exception as e:
            logger.error(f"Failed to decrypt replay request: {e}")
            return

        import re

        id_match = re.search(r"<Id>([^<]+)</Id>", decrypted)
        if not id_match:
            logger.warning(f"No file path in replay request: {decrypted[:200]}")
            return

        file_path_requested = id_match.group(1)
        self._log(f"Replay request: {file_path_requested}")
        logger.info(f"Baichuan replay request for: {file_path_requested}")

        # Look up the file in storage
        local_path = self._storage.get_file(file_path_requested)
        if not local_path:
            logger.error(f"File not found in storage: {file_path_requested}")
            # Send error response
            resp_header = self._build_header(
                cmd_id=5,
                body_length=0,
                channel_id=header["channel_id"],
                msg_num=header["msg_num"],
                response_code=400,
            )
            writer.write(resp_header)
            await writer.drain()
            return

        # Send initial ack: Extension XML with binaryData=1
        ext_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            "<Extension>"
            "<binaryData>1</binaryData>"
            "</Extension>"
        )
        ext_encrypted = encrypt_baichuan(ext_xml, header["channel_id"])
        ack_header = self._build_header(
            cmd_id=5,
            body_length=len(ext_encrypted),
            channel_id=header["channel_id"],
            msg_num=0,
            response_code=200,
        )
        writer.write(ack_header + ext_encrypted)
        await writer.drain()

        # Stream BcMedia-encoded video as push frames
        # Each chunk is sent as a cmd_id=5 message with the binary payload
        # The client reads these with idle timeout to detect end of stream
        chunks_sent = 0
        bytes_sent = 0
        for bcmedia_chunk in encode_file_to_bcmedia(local_path):
            # Send as unencrypted BcMedia data (client handles raw BcMedia)
            frame_header = self._build_header(
                cmd_id=5,
                body_length=len(bcmedia_chunk),
                channel_id=header["channel_id"],
                msg_num=0,
                response_code=200,
                payload_offset=0,
            )
            writer.write(frame_header + bcmedia_chunk)
            chunks_sent += 1
            bytes_sent += len(bcmedia_chunk)

            # Drain periodically to avoid memory buildup
            if chunks_sent % 50 == 0:
                await writer.drain()

        await writer.drain()
        self._log(f"Replay complete: {chunks_sent} chunks, {bytes_sent} bytes")
        logger.info(
            f"Baichuan replay complete: {chunks_sent} chunks, "
            f"{bytes_sent / 1024 / 1024:.1f}MB streamed"
        )
        # Don't close connection -- client detects end via idle timeout

    async def _handle_get_p2p(
        self,
        writer: asyncio.StreamWriter,
        header: dict,
        aes_key: Optional[bytes],
    ):
        """Handle cmd_id=114: P2P/UID discovery."""
        uid = "SIM" + secrets.token_hex(8).upper()
        response_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            "<body>"
            '<P2p version="1.1">'
            f"<uid>{uid}</uid>"
            "</P2p>"
            "</body>"
        )

        if aes_key:
            from .crypto import aes_encrypt

            body = aes_encrypt(response_xml.encode("utf8"), aes_key)
        else:
            body = encrypt_baichuan(response_xml, header["channel_id"])

        resp_header = self._build_header(
            cmd_id=114,
            body_length=len(body),
            channel_id=header["channel_id"],
            response_code=200,
        )
        writer.write(resp_header + body)
        await writer.drain()
