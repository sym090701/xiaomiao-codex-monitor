"""Headless quota collector; speaks the existing XiaoMiao snapshot protocol."""
import argparse
import asyncio
import contextlib
import copy
import json
import math
import os
import secrets
import time

from aiohttp import web
from machw_archive import CodexUsageArchive, MacHardwareArchive


def normalize(result, now=None):
    now = time.time() if now is None else now
    buckets = result.get("rateLimitsByLimitId") or {}
    bucket = buckets.get("codex") or result.get("rateLimits")
    if not isinstance(bucket, dict):
        raise ValueError("Codex quota bucket unavailable")
    out = {"ok": True, "plan_type": bucket.get("planType") or "CODEX",
           "five_hour": None, "weekly": None}
    for field in ("primary", "secondary"):
        window = bucket.get(field)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        target = {300: "five_hour", 10080: "weekly"}.get(duration)
        used = window.get("usedPercent")
        reset = window.get("resetsAt")
        if target and isinstance(used, (int, float)):
            used = max(0, min(100, used))
            out[target] = {"used_percent": used, "remaining_percent": 100 - used,
                           "limit_window_seconds": duration * 60,
                           "reset_at_epoch": reset,
                           "reset_after_seconds": max(0, int(reset - now))
                           if isinstance(reset, (int, float)) else -1}
    return out


async def read_limits(binary):
    proc = await asyncio.create_subprocess_exec(
        binary, "app-server", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()
    async def reply(request_id):
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("Codex app-server stopped")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    # Do not forward upstream errors or account metadata to devices.
                    raise RuntimeError("Codex quota query failed; check server login")
                return message["result"]
    try:
        async with asyncio.timeout(30):
            await send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "xiaomiao_quota", "version": "0.1.0"}}})
            await reply(1)
            await send({"method": "initialized", "params": {}})
            await send({"id": 2, "method": "account/rateLimits/read"})
            return normalize(await reply(2))
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()


def validate_metrics(payload):
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("Expected a successful metrics object")
    out = {"ok": True}
    for key in ("cpu_percent", "memory_percent", "memory_pressure_percent", "disk_percent",
                "cpu_count", "load_1", "net_rx_mbps", "net_tx_mbps", "uptime_seconds",
                "memory_used_gb", "memory_total_gb", "disk_used_gb", "disk_total_gb"):
        value = payload.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Missing or invalid metric: " + key)
        if key.endswith("_percent") and value > 100:
            raise ValueError("Percentage out of range")
        out[key] = value
    name = payload.get("host_name")
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError("Invalid host name")
    out["host_name"] = name
    return out


class Collector:
    def __init__(self, binary, interval):
        self.binary, self.interval = binary, interval
        self.payload = {"ok": False, "error": "Quota warming up"}
        self.updated = None
        self.failed = False
        self.mac_metrics = None
        self.mac_seen = None
        self.archive = MacHardwareArchive.from_env()
        self.codex_archive = CodexUsageArchive.from_env()

    def performance(self):
        if self.mac_seen is None or time.monotonic() - self.mac_seen > 45:
            return {"ok": False, "error": "Mac offline / asleep", "stale": True}
        return {**self.mac_metrics, "stale": False,
                "age_seconds": int(time.monotonic() - self.mac_seen)}

    async def poll(self):
        while True:
            try:
                fresh = await read_limits(self.binary)
                captured = time.time()
                self.payload = fresh
                self.updated, self.failed = captured, False
                self.codex_archive.record(fresh, captured)
            except (OSError, RuntimeError, ValueError, KeyError, asyncio.TimeoutError):
                self.failed = True
            await asyncio.sleep(self.interval)

    def snapshot(self):
        usage = copy.deepcopy(self.payload)
        stale = self.failed or self.updated is None or time.time() - self.updated > self.interval * 3
        usage["stale"] = stale
        usage["server_updated_epoch"] = self.updated
        if stale:
            usage["ok"] = False
            usage["error"] = "Quota stale; check server login/network"
        for name in ("five_hour", "weekly"):
            window = usage.get(name)
            if window and isinstance(window.get("reset_at_epoch"), (int, float)):
                window["reset_after_seconds"] = max(0, int(window["reset_at_epoch"] - time.time()))
        return {"type": "snapshot", "screen_on": True, "usage": usage,
                "performance": self.performance(),
                "codex": {"state": "stopped"}}


def create_app(collector, ingest_token=None):
    app = web.Application(client_max_size=8192)
    async def usage(_request):
        return web.json_response(collector.snapshot()["usage"])
    async def health(_request):
        return web.json_response({"service": "xiaomiao-quota", "quota_ok": collector.snapshot()["usage"]["ok"],
                                  "mac_online": collector.performance()["ok"]})
    async def ingest(request):
        if not ingest_token:
            raise web.HTTPServiceUnavailable(text="Mac ingest not configured")
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth.encode(), ("Bearer " + ingest_token).encode()):
            raise web.HTTPUnauthorized(text="Unauthorized")
        try:
            metrics = validate_metrics(await request.json())
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid metrics")
        collector.mac_metrics, collector.mac_seen = metrics, time.monotonic()
        collector.archive.record(metrics)
        return web.json_response({"ok": True})
    async def websocket(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        async def publish():
            while not ws.closed:
                await ws.send_json(collector.snapshot())
                await asyncio.sleep(5)
        task = asyncio.create_task(publish())
        try:
            async for _ in ws:
                pass
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                await task
        return ws
    async def lifetime(_app):
        task = asyncio.create_task(collector.poll())
        sync_tasks = []
        if collector.archive.enabled:
            sync_tasks.append(asyncio.create_task(collector.archive.sync_loop()))
        if collector.codex_archive.enabled:
            sync_tasks.append(asyncio.create_task(collector.codex_archive.sync_loop()))
        yield
        task.cancel()
        for sync_task in sync_tasks:
            sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for sync_task in sync_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await sync_task
    app.cleanup_ctx.append(lifetime)
    app.router.add_get("/health", health)
    app.router.add_get("/usage", usage)
    app.router.add_get("/ws", websocket)
    app.router.add_post("/api/mac/metrics", ingest)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--codex", default=os.environ.get("CODEX_BINARY", "codex"))
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("interval must be at least 30 seconds")
    token_file = os.environ.get("QUOTA_INGEST_TOKEN_FILE")
    token = None
    if token_file:
        from pathlib import Path
        token = Path(token_file).read_text().strip()
        if len(token) < 32:
            parser.error("Mac ingest token must be at least 32 characters")
    web.run_app(create_app(Collector(args.codex, args.interval), token), host=args.host, port=args.port)
