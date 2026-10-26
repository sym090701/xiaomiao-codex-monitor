"""Headless quota collector; speaks the existing XiaoMiao snapshot protocol."""
import argparse
import asyncio
import contextlib
import copy
import json
import os
import time

from aiohttp import web


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


class Collector:
    def __init__(self, binary, interval):
        self.binary, self.interval = binary, interval
        self.payload = {"ok": False, "error": "Quota warming up"}
        self.updated = None
        self.failed = False

    async def poll(self):
        while True:
            try:
                self.payload = await read_limits(self.binary)
                self.updated, self.failed = time.time(), False
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
                "performance": {"ok": False, "error": "Mac metrics unavailable in server mode"},
                "codex": {"state": "stopped"}}


def create_app(collector):
    app = web.Application()
    async def usage(_request):
        return web.json_response(collector.snapshot()["usage"])
    async def health(_request):
        return web.json_response({"service": "xiaomiao-quota", "quota_ok": collector.snapshot()["usage"]["ok"]})
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
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    app.cleanup_ctx.append(lifetime)
    app.router.add_get("/health", health)
    app.router.add_get("/usage", usage)
    app.router.add_get("/ws", websocket)
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
    web.run_app(create_app(Collector(args.codex, args.interval)), host=args.host, port=args.port)
