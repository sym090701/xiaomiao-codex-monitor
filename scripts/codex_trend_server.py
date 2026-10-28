#!/usr/bin/env python3
"""Read-only short-term Codex trend API for XiaoMiao on the NAS."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from urllib.parse import parse_qs, urlparse


def load_points(root, hours=4, limit=48):
    cutoff = time.time() - hours * 3600
    rows = []
    for path in sorted(root.glob("*.ndjson"))[-3:]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                usage = row.get("usage", {})
                epoch = float(row.get("observed_at_epoch", row.get("captured_at_epoch", 0)))
                five = usage.get("five_hour") or {}
                weekly = usage.get("weekly") or {}
                if epoch < cutoff or not isinstance(five.get("remaining_percent"), (int, float)):
                    continue
                if not isinstance(weekly.get("remaining_percent"), (int, float)):
                    continue
                rows.append({
                    "observed_at_epoch": epoch,
                    "observed_at_local": row.get("observed_at_local", ""),
                    "five_hour_remaining": max(0.0, min(100.0, float(five["remaining_percent"]))),
                    "weekly_remaining": max(0.0, min(100.0, float(weekly["remaining_percent"]))),
                })
                reset_after = five.get("reset_after_seconds")
                if isinstance(reset_after, (int, float)) and reset_after >= 0:
                    rows[-1]["five_hour_reset_at_epoch"] = epoch + float(reset_after)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    rows.sort(key=lambda row: row["observed_at_epoch"])
    if len(rows) > limit:
        indexes = [round(i * (len(rows) - 1) / (limit - 1)) for i in range(limit)]
        rows = [rows[index] for index in indexes]
    return rows


def summarize(points):
    if len(points) < 2:
        return {"rate_percent_per_hour": 0.0, "eta_hours": None,
                "freshness_seconds": None, "latest_reset_after_seconds": None}
    first, last = points[0], points[-1]
    active_seconds = 0.0
    total_drop = 0.0
    active_segments = 0
    for previous, current in zip(points, points[1:]):
        elapsed = current["observed_at_epoch"] - previous["observed_at_epoch"]
        drop = previous["five_hour_remaining"] - current["five_hour_remaining"]
        # Ignore flat/near-flat intervals: they represent waiting between bursts.
        if elapsed > 0 and drop >= 0.2:
            active_seconds += elapsed
            total_drop += drop
            active_segments += 1
    rate = total_drop * 3600.0 / active_seconds if active_seconds > 0 else 0.0
    eta = last["five_hour_remaining"] / rate if rate > 0.05 else None
    reset_after = None
    if "five_hour_reset_at_epoch" in last:
        reset_after = max(0.0, last["five_hour_reset_at_epoch"] - time.time())
    return {"rate_percent_per_hour": round(rate, 3), "eta_hours": round(eta, 2) if eta is not None else None,
            "active_segments": active_segments,
            "freshness_seconds": max(0.0, time.time() - last["observed_at_epoch"]),
            "latest_reset_after_seconds": reset_after}


def make_handler(root):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                payload = {"status": "ok", "service": "codex-trend"}
            elif parsed.path == "/api/codex-trend":
                query = parse_qs(parsed.query)
                try:
                    hours = max(1, min(24, int(query.get("hours", [4])[0])))
                    limit = max(2, min(48, int(query.get("points", [48])[0])))
                except (TypeError, ValueError):
                    self.send_error(400, "invalid hours or points")
                    return
                points = load_points(root, hours, limit)
                payload = {"schema_version": 1, "timezone": "Asia/Shanghai",
                           "window_hours": hours, "points": points, **summarize(points)}
            else:
                self.send_error(404)
                return
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/vol3/1000/512/codexuselogs"))
    parser.add_argument("--host", default="192.168.1.158")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), make_handler(args.root)).serve_forever()
