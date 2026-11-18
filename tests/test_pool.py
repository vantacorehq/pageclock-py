import random
import unittest

from pageclock import (
    BadFrame,
    Checksum,
    FlushFailed,
    Full,
    LoadFailed,
    NotPinned,
    Pool,
    crc32,
)


def load_id(page_id: int):
    def load(buf: bytearray) -> None:
        buf[:8] = page_id.to_bytes(8, "little")

    return load


def ok_flush(_page_id: int, _image: bytes) -> None:
    return None


class PoolTests(unittest.TestCase):
    def test_crc_known_answer(self) -> None:
        self.assertEqual(crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(crc32(b""), 0)
        self.assertNotEqual(crc32(b"\x00"), crc32(b"\x01"))

    def test_hit_does_not_reload(self) -> None:
        pool = Pool(32, 2)
        loads = 0

        def load(buf: bytearray) -> None:
            nonlocal loads
            loads += 1
            buf[:8] = (4).to_bytes(8, "little")

        frame = pool.pin(4, load, ok_flush)
        pool.unpin(frame)
        again = pool.pin(4, load_id(4), ok_flush)
        self.assertEqual(frame, again)
        self.assertEqual(loads, 1)
        self.assertEqual(pool.stats().hits, 1)
        self.assertEqual(pool.stats().misses, 1)
        self.assertEqual(pool.pins(frame), 1)

    def test_double_pin_and_extra_unpin(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        self.assertEqual(pool.pin(1, load_id(1), ok_flush), frame)
        self.assertEqual(pool.pins(frame), 2)
        pool.unpin(frame)
        pool.unpin(frame)
        self.assertEqual(pool.pins(frame), 0)
        with self.assertRaises(NotPinned):
            pool.unpin(frame)

    def test_pinned_page_is_not_evicted(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        loaded = False

        def load(_buf: bytearray) -> None:
            nonlocal loaded
            loaded = True

        with self.assertRaises(Full):
            pool.pin(2, load, ok_flush)
        self.assertFalse(loaded)
        self.assertEqual(pool.page_id(frame), 1)
        self.assertEqual(pool.resident_count(), 1)

    def test_second_chance_then_evict_the_cold_page(self) -> None:
        pool = Pool(32, 2)
        first = pool.pin(1, load_id(1), ok_flush)
        pool.unpin(first)
        second = pool.pin(2, load_id(2), ok_flush)
        pool.unpin(second)

        victim = pool.pin(3, load_id(3), ok_flush)
        self.assertEqual(pool.page_id(victim), 3)
        self.assertEqual(pool.stats().evictions, 1)
        images = [bytes(pool.bytes(0)[:8]), bytes(pool.bytes(1)[:8])]
        self.assertIn((3).to_bytes(8, "little"), images)
        survivor = 0 if pool.page_id(0) == 2 else 1
        self.assertEqual(pool.page_id(survivor), 2)
        self.assertEqual(bytes(pool.bytes(survivor)[:8]), (2).to_bytes(8, "little"))

    def test_dirty_eviction_flushes_exact_bytes(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        pool.bytes_mut(frame)[:4] = b"\x09\x09\x09\x09"
        pool.unpin(frame)
        flushed = []

        def flush(page_id: int, image: bytes) -> None:
            flushed.append((page_id, image[:4]))

        frame = pool.pin(2, load_id(2), flush)
        self.assertEqual(flushed, [(1, b"\x09\x09\x09\x09")])
        self.assertEqual(pool.stats().flushes, 1)
        self.assertEqual(pool.page_id(frame), 2)
        self.assertFalse(pool.is_dirty(frame))

    def test_failed_flush_keeps_the_dirty_page(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        pool.mark_dirty(frame)
        pool.unpin(frame)

        def flush(_page_id: int, _image: bytes) -> None:
            raise FlushFailed

        with self.assertRaises(FlushFailed):
            pool.pin(2, load_id(2), flush)
        self.assertEqual(pool.page_id(frame), 1)
        self.assertTrue(pool.is_dirty(frame))
        self.assertEqual(pool.stats().evictions, 0)
        self.assertEqual(pool.stats().misses, 1)

        again = pool.pin(1, load_id(99), ok_flush)
        self.assertEqual(again, frame)
        self.assertEqual(pool.stats().hits, 1)

    def test_failed_load_does_not_drop_resident_page(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        pool.unpin(frame)

        def load(_buf: bytearray) -> None:
            raise LoadFailed

        with self.assertRaises(LoadFailed):
            pool.pin(2, load, ok_flush)
        self.assertEqual(pool.page_id(frame), 1)
        self.assertEqual(bytes(pool.bytes(frame)[:8]), (1).to_bytes(8, "little"))
        self.assertEqual(pool.stats().evictions, 0)
        self.assertEqual(pool.resident_count(), 1)

    def test_clean_eviction_does_not_flush(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        pool.unpin(frame)
        flushed = False

        def flush(_page_id: int, _image: bytes) -> None:
            nonlocal flushed
            flushed = True

        pool.pin(2, load_id(2), flush)
        self.assertFalse(flushed)
        self.assertEqual(pool.stats().flushes, 0)
        self.assertEqual(pool.stats().evictions, 1)

    def test_seal_and_verify_detect_a_flipped_bit(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        pool.seal(frame)
        pool.verify(frame)
        pool.bytes_mut(frame)[0] ^= 0x01
        with self.assertRaises(Checksum):
            pool.verify(frame)

    def test_bytes_mut_requires_a_pin(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        pool.unpin(frame)
        with self.assertRaises(NotPinned):
            pool.bytes_mut(frame)
        self.assertEqual(len(pool.bytes(frame)), 32)

    def test_read_view_is_not_writable(self) -> None:
        pool = Pool(32, 1)
        frame = pool.pin(1, load_id(1), ok_flush)
        view = pool.bytes(frame)
        with self.assertRaises(TypeError):
            view[0] = 1

    def test_disk_oracle_under_a_fixed_schedule(self) -> None:
        """A tiny disk is the source of truth. The pool may not invent bytes."""
        pool = Pool(64, 3)
        disk: dict[int, bytes] = {}
        rng = random.Random(0xC10C)

        def load_from_disk(page_id: int, buf: bytearray) -> None:
            image = disk.get(page_id)
            if image is None:
                image = bytes(rng.getrandbits(8) for _ in range(pool.page_size))
                disk[page_id] = image
            buf[:] = image

        def flush_to_disk(page_id: int, image: bytes) -> None:
            if rng.randrange(17) == 0:
                raise FlushFailed
            disk[page_id] = image

        pinned: list[int] = []
        for step in range(500):
            page_id = rng.randrange(8)
            try:
                frame = pool.pin(
                    page_id,
                    lambda buf, page_id=page_id: load_from_disk(page_id, buf),
                    flush_to_disk,
                )
            except Full:
                self.assertTrue(pinned)
                pool.unpin(pinned.pop())
                continue
            except FlushFailed:
                self._assert_clean_frames_match(pool, disk)
                if pinned:
                    pool.unpin(pinned.pop())
                continue

            self.assertEqual(pool.page_id(frame), page_id)
            if not pool.is_dirty(frame):
                self.assertEqual(bytes(pool.bytes(frame)), disk[page_id])
            if rng.randrange(3) == 0:
                mutable = pool.bytes_mut(frame)
                mutable[0] = (mutable[0] + 1) % 256
            if rng.randrange(2) == 0:
                pool.unpin(frame)
            else:
                pinned.append(frame)
            if step % 11 == 0:
                while pinned:
                    pool.unpin(pinned.pop())

        while pinned:
            pool.unpin(pinned.pop())
        self._assert_clean_frames_match(pool, disk)

    def _assert_clean_frames_match(self, pool: Pool, disk: dict[int, bytes]) -> None:
        for index in range(pool.frame_count):
            try:
                page_id = pool.page_id(index)
            except BadFrame:
                continue
            if not pool.is_dirty(index):
                self.assertEqual(bytes(pool.bytes(index)), disk[page_id])


if __name__ == "__main__":
    unittest.main()
