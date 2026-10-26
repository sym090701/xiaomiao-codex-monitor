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
    async def test_mac_auth_validation_and_expiry(self):
        collector = Collector("unused", 60)
        async def idle():
            import asyncio
            await asyncio.Future()
        token = "test-fixture-only-not-a-real-token-000000"
        metrics = {key: 1 for key in ("cpu_percent", "memory_percent", "memory_pressure_percent", "disk_percent",
                   "cpu_count", "load_1", "net_rx_mbps", "net_tx_mbps", "uptime_seconds",
                   "memory_used_gb", "memory_total_gb", "disk_used_gb", "disk_total_gb")}
        metrics.update(ok=True, host_name="test-mac", account_token="must-be-dropped")
        with patch.object(collector, "poll", idle):
            async with TestClient(TestServer(create_app(collector, token))) as client:
                response = await client.post("/api/mac/metrics", json=metrics)
                self.assertEqual(response.status, 401)
                headers = {"Authorization": "Bearer " + token}
                response = await client.post("/api/mac/metrics", json=metrics, headers=headers)
                self.assertEqual(response.status, 200)
                self.assertTrue(collector.performance()["ok"])
                self.assertNotIn("account_token", collector.performance())
                with patch("quota_server.time.monotonic", return_value=collector.mac_seen + 46):
                    self.assertFalse(collector.performance()["ok"])
                invalid = {**metrics, "cpu_percent": 101}
                response = await client.post("/api/mac/metrics", json=invalid, headers=headers)
                self.assertEqual(response.status, 400)

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
