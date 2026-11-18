"""ISO-HDLC CRC-32. Same trailer the Rust crate writes."""

from __future__ import annotations

import zlib


def crc32(data: bytes | bytearray | memoryview) -> int:
    """Checksum in the unsigned 32-bit range.

    Known answer: ``crc32(b"123456789") == 0xCBF43926``.
    """
    return zlib.crc32(data) & 0xFFFFFFFF
