"""Single-threaded clock-sweep pool over fixed-size pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pageclock.crc import crc32


class PoolError(Exception):
    """Base type for refusals that leave the pool unchanged, apart from clock bits."""


class Full(PoolError):
    """Every frame is pinned, so the clock cannot take one."""


class BadFrame(PoolError):
    """The index is out of range, or the frame holds no page."""


class NotPinned(PoolError):
    """The frame is not pinned."""


class Checksum(PoolError):
    """The CRC-32 trailer does not match the page image."""


class LoadFailed(PoolError):
    """``load`` rejected the page. No frame was replaced."""


class FlushFailed(PoolError):
    """``flush`` rejected a dirty page. That page is still resident."""


@dataclass(frozen=True)
class Stats:
    """Counters since :meth:`Pool.__init__`. Exact on this thread."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    flushes: int = 0


class _Frame:
    __slots__ = ("page", "pins", "dirty", "referenced", "buf")

    def __init__(self, page_size: int) -> None:
        self.page: int | None = None
        self.pins = 0
        self.dirty = False
        self.referenced = False
        self.buf = bytearray(page_size)


class Pool:
    """``frames`` pages of ``page_size`` bytes each.

    ``page_size`` must be at least 8. ``frames`` must be at least 1.

    ``load`` and ``flush`` must not call back into the same pool.
    ``flush`` receives a snapshot (``bytes``), not a live view of the frame.
    :meth:`bytes_mut` marks the frame dirty. Eviction calls ``flush`` with
    that image before the frame is reused. If ``flush`` raises
    :class:`FlushFailed`, the resident page stays. If ``load`` raises
    :class:`LoadFailed`, nothing is evicted.
    """

    __slots__ = (
        "_page_size",
        "_frames",
        "_scratch",
        "_hand",
        "_hits",
        "_misses",
        "_evictions",
        "_flushes",
    )

    def __init__(self, page_size: int, frames: int) -> None:
        if frames < 1:
            raise ValueError("pageclock needs at least one frame")
        if page_size < 8:
            raise ValueError("pageclock pages must be at least 8 bytes")
        self._page_size = page_size
        self._frames = [_Frame(page_size) for _ in range(frames)]
        self._scratch = bytearray(page_size)
        self._hand = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._flushes = 0

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    def stats(self) -> Stats:
        return Stats(self._hits, self._misses, self._evictions, self._flushes)

    def resident_count(self) -> int:
        return sum(frame.page is not None for frame in self._frames)

    def pin(
        self,
        page_id: int,
        load: Callable[[bytearray], None],
        flush: Callable[[int, bytes], None],
    ) -> int:
        """Pin ``page_id``, loading it when it is not resident. Returns the frame index."""
        found = self._find(page_id)
        if found is not None:
            frame = self._frames[found]
            frame.pins += 1
            frame.referenced = True
            self._hits += 1
            return found

        index = self._select_victim()
        frame = self._frames[index]
        if frame.page is not None and frame.dirty:
            try:
                flush(frame.page, bytes(frame.buf))
            except FlushFailed:
                raise
            frame.dirty = False
            self._flushes += 1

        self._scratch[:] = b"\x00" * self._page_size
        try:
            load(self._scratch)
        except LoadFailed:
            raise

        replaced = frame.page
        frame.buf[:] = self._scratch
        frame.page = page_id
        frame.pins = 1
        frame.dirty = False
        frame.referenced = True
        if replaced is not None:
            self._evictions += 1
        self._misses += 1
        self._hand = (index + 1) % len(self._frames)
        return index

    def unpin(self, frame: int) -> None:
        slot = self._slot(frame)
        if slot.page is None or slot.pins == 0:
            raise NotPinned
        slot.pins -= 1

    def mark_dirty(self, frame: int) -> None:
        self._pinned(frame).dirty = True

    def is_dirty(self, frame: int) -> bool:
        return self._occupied(frame).dirty

    def pins(self, frame: int) -> int:
        return self._occupied(frame).pins

    def page_id(self, frame: int) -> int:
        page = self._occupied(frame).page
        assert page is not None
        return page

    def bytes(self, frame: int) -> memoryview:
        """Read-only view of a resident page. The frame need not be pinned."""
        view = memoryview(self._occupied(frame).buf)
        return view.toreadonly()

    def bytes_mut(self, frame: int) -> bytearray:
        """Live buffer of a pinned page. Marks the page dirty."""
        slot = self._pinned(frame)
        slot.dirty = True
        slot.referenced = True
        return slot.buf

    def seal(self, frame: int) -> None:
        """Write CRC-32 of ``buf[:-4]`` into the last four bytes. Marks dirty."""
        slot = self._pinned(frame)
        digest = crc32(slot.buf[:-4])
        slot.buf[-4:] = digest.to_bytes(4, "little")
        slot.dirty = True

    def verify(self, frame: int) -> None:
        slot = self._occupied(frame)
        digest = crc32(slot.buf[:-4])
        stored = int.from_bytes(slot.buf[-4:], "little")
        if digest != stored:
            raise Checksum

    def _find(self, page_id: int) -> int | None:
        for index, frame in enumerate(self._frames):
            if frame.page == page_id:
                return index
        return None

    def _slot(self, index: int) -> _Frame:
        if index < 0 or index >= len(self._frames):
            raise BadFrame
        return self._frames[index]

    def _occupied(self, index: int) -> _Frame:
        frame = self._slot(index)
        if frame.page is None:
            raise BadFrame
        return frame

    def _pinned(self, index: int) -> _Frame:
        frame = self._occupied(index)
        if frame.pins == 0:
            raise NotPinned
        return frame

    def _select_victim(self) -> int:
        count = len(self._frames)
        for step in range(count):
            index = (self._hand + step) % count
            if self._frames[index].page is None:
                return index
        for _ in range(count * 2):
            index = self._hand
            self._hand = (self._hand + 1) % count
            frame = self._frames[index]
            if frame.pins > 0:
                continue
            if frame.referenced:
                frame.referenced = False
                continue
            return index
        raise Full()
