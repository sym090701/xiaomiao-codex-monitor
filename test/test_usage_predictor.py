import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from usage_predictor import predict_usage


class UsagePredictorTest(unittest.TestCase):
    zone = ZoneInfo("Asia/Shanghai")

    def epoch(self, value):
        return dt.datetime.fromisoformat(value).replace(tzinfo=self.zone).timestamp()

    def write_day(self, root, day, hour=9, preheat=False):
        epoch = self.epoch(day + "T%02d:00:00" % hour)
        rows = [dict(observed_at_epoch=epoch - 60, record_type="codex_usage",
                     usage={"five_hour": {"remaining_percent": 100}}),
                dict(observed_at_epoch=epoch, record_type="codex_preheat" if preheat else "codex_usage",
                     usage={"five_hour": {"remaining_percent": 99}}),
                dict(observed_at_epoch=epoch + 60, record_type="codex_usage",
                     usage={"five_hour": {"remaining_percent": 98 if preheat else 99}})]
        Path(root, day + ".ndjson").write_text("\n".join(json.dumps(row) for row in rows))

    def write_complete_day(self, root, day, hour=9, duration_minutes=90):
        start = self.epoch(day + "T%02d:00:00" % hour)
        rows = []
        for minute in range(duration_minutes + 1):
            rows.append(dict(observed_at_epoch=start + minute * 60,
                             record_type="codex_usage",
                             usage={"five_hour": {
                                 "remaining_percent": 100 - minute}}))
        Path(root, day + ".ndjson").write_text("\n".join(json.dumps(row) for row in rows))

    def test_workday_prediction_with_three_distinct_days(self):
        with tempfile.TemporaryDirectory() as root:
            for day in ["2026-10-28", "2026-10-29", "2026-10-30"]:
                self.write_day(root, day)
            result = predict_usage(root, self.epoch("2026-11-03T05:30:00"))
            self.assertEqual(result["predicted_at_epoch"], self.epoch("2026-11-03T09:00:00"))
            self.assertEqual(result["support_days"], 3)
            self.assertEqual(result["basis"], "workday")

    def test_cross_midnight_lead_uses_tomorrow(self):
        with tempfile.TemporaryDirectory() as root:
            for day in ["2026-10-28", "2026-10-29", "2026-10-30"]:
                self.write_day(root, day, hour=1)
            result = predict_usage(root, self.epoch("2026-11-03T21:30:00"))
            self.assertEqual(result["predicted_at_epoch"] - 12600,
                             self.epoch("2026-11-03T21:30:00"))

    def test_preheat_and_following_sample_do_not_teach_usage(self):
        with tempfile.TemporaryDirectory() as root:
            for day in ["2026-10-28", "2026-10-29", "2026-10-30"]:
                self.write_day(root, day, preheat=True)
            result = predict_usage(root, self.epoch("2026-11-03T05:30:00"))
            self.assertIsNone(result["predicted_at_epoch"])

    def test_scattered_starts_are_not_a_confident_habit(self):
        with tempfile.TemporaryDirectory() as root:
            for day, hour in [("2026-10-28", 8), ("2026-10-29", 13), ("2026-10-30", 21)]:
                self.write_day(root, day, hour=hour)
            result = predict_usage(root, self.epoch("2026-11-03T05:30:00"))
            self.assertIsNone(result["predicted_at_epoch"])

    def test_dynamic_lead_uses_complete_session_duration(self):
        with tempfile.TemporaryDirectory() as root:
            for day in ["2026-10-06", "2026-10-13", "2026-10-20", "2026-10-27"]:
                self.write_complete_day(root, day, duration_minutes=90)
            result = predict_usage(root, self.epoch("2026-11-03T05:30:00"))
            self.assertEqual(result["lead_basis"], "historical_duration")
            self.assertEqual(result["duration_hours"], 1.48)
            self.assertEqual(result["lead_seconds"], 13500)
