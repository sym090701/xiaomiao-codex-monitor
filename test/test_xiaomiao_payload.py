"""Offline checks for the payload consumed by the XiaoMiao firmware."""
import unittest
import main


class XiaoMiaoPayloadTest(unittest.TestCase):
    def test_windows_classified_by_duration_even_when_reversed(self):
        payload = main.parse_usage({"rate_limit": {
            "primary_window": {"used_percent": 80, "limit_window_seconds": 604800,
                               "reset_after_seconds": 120},
            "secondary_window": {"used_percent": 25, "limit_window_seconds": 18000,
                                 "reset_after_seconds": 60},
        }})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["five_hour"]["remaining_percent"], 75)
        self.assertEqual(payload["weekly"]["remaining_percent"], 20)
        self.assertEqual(payload["five_hour"]["reset_after_seconds"], 60)

    def test_absent_weekly_is_unknown_not_empty_quota(self):
        payload = main.parse_usage({"rate_limit": {
            "primary_window": {"used_percent": 0, "limit_window_seconds": 18000}
        }})
        self.assertEqual(payload["five_hour"]["remaining_percent"], 100)
        self.assertIsNone(payload["weekly"]["remaining_percent"])


if __name__ == "__main__":
    unittest.main()
