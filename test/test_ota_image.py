import hashlib
import struct
import unittest
from scripts.ota_upload import image_digest, MARKER


class OtaImageTest(unittest.TestCase):
    def image(self, marker=MARKER):
        header = bytearray(24)
        header[0], header[1], header[23] = 0xe9, 1, 1
        payload = marker.ljust(300, b"x")
        data = bytes(header) + struct.pack("<II", 0x3f400020, len(payload)) + payload
        checksum = 0xef
        for b in payload:
            checksum ^= b
        data = data.ljust(((len(data) + 16) & ~15) - 1, b"\0") + bytes([checksum])
        return data + hashlib.sha256(data).digest()

    def test_valid_image(self):
        data = self.image()
        self.assertEqual(image_digest(data), hashlib.sha256(data[:-32]).hexdigest())

    def test_reject_corruption_merged_and_wrong_target(self):
        valid = self.image()
        corrupt = bytearray(valid)
        corrupt[70] ^= 1
        for data in (bytes(corrupt), b"\xff" * 4096 + valid, self.image(b"other-project"), valid[:-8]):
            with self.assertRaises(ValueError):
                image_digest(data)
