import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from machw_archive import CodexUsageArchive, MacHardwareArchive


class MacHardwareArchiveTest(unittest.TestCase):
    def test_record_is_daily_ndjson_with_private_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = MacHardwareArchive(folder)
            archive.record({"host_name": "My Mac/测试", "cpu_percent": 12}, received_epoch=1793000000)
            files = list(Path(folder).rglob("*.ndjson"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["host_name"], "My Mac/测试")
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(files[0].parent.stat().st_mode & 0o777, 0o700)

    def test_no_remote_sync_is_a_noop(self):
        async def run():
            archive = MacHardwareArchive(tempfile.mkdtemp())
            self.assertFalse(await archive.sync_once())
        asyncio.run(run())

    def test_codex_usage_is_daily_nested_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = CodexUsageArchive(folder)
            archive.record({"ok": True, "five_hour": {"remaining_percent": 74}},
                           captured_epoch=1793000000)
            files = list(Path(folder).glob("*.ndjson"))
            self.assertEqual(len(files), 1)
            row = json.loads(files[0].read_text())
            self.assertEqual(row["usage"]["five_hour"]["remaining_percent"], 74)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
