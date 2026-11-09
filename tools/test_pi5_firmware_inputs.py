#!/usr/bin/env python3
"""Guard firmware transport and on-disk partition selection before guest execution."""
from pathlib import Path
import struct
import tempfile
import unittest
import uuid
import zlib

from run_pi5_firmware import firmware_transport, partition_start, regular


class Inputs(unittest.TestCase):
    def image(self, root):
        data = bytearray(1024 * 1024)
        entries = bytearray(128 * 128)
        for index, (kind, name, first, last) in enumerate([
            ('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', 'EFISYS', 34, 999),
            ('ebd0a0a2-b9e5-4433-87c0-68b6b72699c7', 'DATRACE', 1000, 1999),
        ]):
            offset = index * 128
            entries[offset:offset + 16] = uuid.UUID(kind).bytes_le
            entries[offset + 16:offset + 32] = uuid.uuid4().bytes_le
            struct.pack_into('<QQ', entries, offset + 32, first, last)
            encoded = name.encode('utf-16le')
            entries[offset + 56:offset + 56 + len(encoded)] = encoded
        data[1024:1024 + len(entries)] = entries
        header = bytearray(512)
        header[:8] = b'EFI PART'
        struct.pack_into('<III', header, 8, 0x10000, 92, 0)
        struct.pack_into('<QQQQ', header, 24, 1, 2047, 34, 2014)
        header[56:72] = uuid.uuid4().bytes_le
        struct.pack_into('<QIII', header, 72, 2, 128, 128, zlib.crc32(entries))
        struct.pack_into('<I', header, 16, zlib.crc32(header[:92]))
        data[512:1024] = header
        path = root / 'card.img'
        path.write_bytes(data)
        return path

    def test_partition_selection_uses_whole_disk_lbas(self):
        with tempfile.TemporaryDirectory() as temp:
            image = self.image(Path(temp))
            self.assertEqual(partition_start(image), 34)
            self.assertEqual(partition_start(image, 'DATRACE'), 1000)
            with self.assertRaisesRegex(ValueError, 'requested partition'):
                partition_start(image, 'MISSING')

    def test_corrupt_gpt_and_partition_only_images_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = self.image(root)
            original = image.read_bytes()
            data = bytearray(original)
            data[1024 + 32] ^= 1
            image.write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'table checksum'):
                partition_start(image)
            image.write_bytes(original[34 * 512:1000 * 512])
            with self.assertRaisesRegex(ValueError, 'GPT SD image'):
                partition_start(image)

    def test_transport_preserves_firmware_and_aligned_entry(self):
        raw = struct.pack('<I', 0x14000020) + bytes(range(256)) * 20
        wrapped = firmware_transport(raw)
        self.assertEqual(wrapped[4096:], raw)
        branch = struct.unpack_from('<I', wrapped)[0]
        offset = struct.unpack_from('<Q', wrapped, 8)[0]
        self.assertEqual((branch & 0x3ffffff) * 4, 4096)
        self.assertEqual(offset + 4096, 0x80000)
        self.assertEqual(wrapped[56:60], b'ARM\x64')

    def test_non_raw_firmware_and_devices_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'entry branch'):
            firmware_transport(b'\x7fELF' + bytes(8192))
        with self.assertRaisesRegex(ValueError, 'size'):
            firmware_transport(b'\0' * 128)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            regular(Path('/dev/null'))


if __name__ == '__main__':
    unittest.main()
