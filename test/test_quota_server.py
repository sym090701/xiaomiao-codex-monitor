import unittest
from unittest.mock import patch
from quota_server import normalize, Collector, create_app
from aiohttp.test_utils import TestClient, TestServer


class QuotaServerTest(unittest.TestCase):
    def test_zero_and_missing_windows(self):
        usage = normalize({"rateLimits": {"primary": {
            "usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1000}}}, now=900)
        self.assertEqual(usage["five_hour"]["remaining_percent"], 100)
        self.assertEqual(usage["five_hour"]["reset_after_seconds"], 100)
        self.assertIsNone(usage["weekly"])

    def test_select_codex_bucket_and_classify_reversed_windows(self):
        usage = normalize({"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 20, "windowDurationMins": 10080},
            "secondary": {"usedPercent": 40, "windowDurationMins": 300}}}})
        self.assertEqual(usage["weekly"]["remaining_percent"], 80)
        self.assertEqual(usage["five_hour"]["remaining_percent"], 60)

    def test_stale_cache_never_claims_live_quota(self):
        collector = Collector("unused", 60)
        collector.payload = {"ok": True, "five_hour": {"reset_at_epoch": 1000}}
        collector.updated = 900
        with patch("quota_server.time.time", return_value=920):
            self.assertEqual(collector.snapshot()["usage"]["five_hour"]["reset_after_seconds"], 80)
            self.assertTrue(collector.snapshot()["usage"]["ok"])
            collector.failed = True
            self.assertFalse(collector.snapshot()["usage"]["ok"])
        self.assertNotIn("reset_after_seconds", collector.payload["five_hour"])


class TransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_device_receives_snapshot_and_no_mac_metrics(self):
        collector = Collector("unused", 60)
        collector.payload = {"ok": True, "five_hour": None, "weekly": None}
        collector.updated = 10**12
        async def idle():
            import asyncio
            await asyncio.Future()
        with patch.object(collector, "poll", idle):
            async with TestClient(TestServer(create_app(collector))) as client:
                response = await client.get("/usage")
                self.assertTrue((await response.json())["ok"])
                async with client.ws_connect("/ws?device=codexusage") as ws:
                    payload = await ws.receive_json(timeout=2)
                    self.assertEqual(payload["type"], "snapshot")
                    self.assertTrue(payload["screen_on"])
                    self.assertFalse(payload["performance"]["ok"])
