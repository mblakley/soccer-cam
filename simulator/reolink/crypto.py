"""Baichuan protocol crypto primitives for Reolink simulator.

Server-side encrypt/decrypt for XOR (XML) and AES-128-CFB (binary payloads).
Extracted from video_grouper/cameras/reolink_download.py -- same constants
and algorithms, but the server needs both directions.
"""

from hashlib import md5

from Crypto.Cipher import AES

AES_IV = b"0123456789abcdef"
XML_KEY = [0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF]


def md5_str_modern(s: str) -> str:
    """MD5 hash: 31 uppercase hex chars (matches reolink-aio format)."""
    return md5(s.encode("utf8")).hexdigest()[:31].upper()


def encrypt_baichuan(buf: str, offset: int) -> bytes:
    """XOR cipher for Baichuan XML payloads (encrypt plaintext string -> bytes)."""
    offset = offset % 256
    result = bytearray()
    for idx, char in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        result.append(ord(char) ^ key ^ offset)
    return bytes(result)


def decrypt_baichuan(buf: bytes, offset: int) -> str:
    """XOR decipher for Baichuan XML payloads (decrypt bytes -> plaintext string)."""
    offset = offset % 256
    result = []
    for idx, byte_val in enumerate(buf):
        key = XML_KEY[(offset + idx) % len(XML_KEY)]
        result.append(chr(byte_val ^ key ^ offset))
    return "".join(result)


def aes_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-CFB encrypt with fixed IV."""
    cipher = AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
    return cipher.encrypt(data)


def aes_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-CFB decrypt with fixed IV."""
    cipher = AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)
    return cipher.decrypt(data)
