"""Clock-sweep buffer pool.

Same rules as the Rust crate `pageclock`: a pinned frame is not a victim,
a dirty frame is flushed before it is reused, a failed flush or load does
not drop the resident page. This package is the policy, not a storage engine.
"""

from pageclock.pool import (
    BadFrame,
    Checksum,
    FlushFailed,
    Full,
    LoadFailed,
    NotPinned,
    Pool,
    PoolError,
    Stats,
)
from pageclock.crc import crc32

__all__ = [
    "BadFrame",
    "Checksum",
    "FlushFailed",
    "Full",
    "LoadFailed",
    "NotPinned",
    "Pool",
    "PoolError",
    "Stats",
    "crc32",
]
