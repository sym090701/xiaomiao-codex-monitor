"""Headless quota collector; speaks the existing XiaoMiao snapshot protocol."""
import argparse
import asyncio
import contextlib
import copy
import json
import math
import os
from pathlib import Path
import secrets
import time
import uuid

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


class Preheater:
    """Run one tiny, tool-free turn in a persistent Codex thread."""

    def __init__(self, binary):
        self.binary = binary
        self.state_file = Path(os.environ.get(
            "PREHEAT_STATE_FILE", "/var/lib/xiaomiao-quota/preheat.json"))
        self.lock = asyncio.Lock()
        self.state = self._load()

    def _load(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)

    def status(self):
        cooldown_until = float(self.state.get("cooldown_until", 0) or 0)
        return {"thread_id_present": bool(self.state.get("thread_id")),
                "last_result": self.state.get("last_result"),
                "last_error": self.state.get("last_error"),
                "last_turn_at": self.state.get("last_turn_at"),
                "cooldown_active": cooldown_until > time.time(),
                "cooldown_remaining_seconds": max(0, int(cooldown_until - time.time()))}

    async def _rpc(self, proc, request_id, method, params):
        proc.stdin.write((json.dumps({"id": request_id, "method": method,
                                      "params": params}) + "\n").encode())
        await proc.stdin.drain()
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 30)
            if not line:
                raise RuntimeError("Codex preheat app-server stopped")
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError("Codex preheat request failed")
            return message.get("result", {})

    async def run(self, collector):
        async with self.lock:
            now = time.time()
            cooldown_until = float(self.state.get("cooldown_until", 0) or 0)
            if now < cooldown_until:
                return {"ok": False, "status": "cooldown",
                        "retry_after_seconds": int(cooldown_until - now)}
            if not collector.payload.get("ok"):
                return {"ok": False, "status": "quota_unavailable"}
            proc = await asyncio.create_subprocess_exec(
                self.binary, "app-server", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                await self._rpc(proc, 1, "initialize", {"clientInfo": {
                    "name": "xiaomiao_preheater", "title": "XiaoMiao quota preheater",
                    "version": "0.1.0"}})
                proc.stdin.write(b'{"method":"initialized","params":{}}\n')
                await proc.stdin.drain()
                thread_id = self.state.get("thread_id")
                if not thread_id:
                    result = await self._rpc(proc, 2, "thread/start", {
                        "cwd": "/var/lib/xiaomiao-quota",
                        "ephemeral": False,
                        "approvalPolicy": "never",
                        "developerInstructions":
                            "This is a quota preheat thread. Never use tools or modify files. Reply only OK.",
                    })
                    thread_id = result.get("thread", {}).get("id")
                    if not thread_id:
                        raise RuntimeError("Codex preheat thread was not created")
                turn_id = str(uuid.uuid4())
                proc.stdin.write((json.dumps({"id": 3, "method": "turn/start",
                    "params": {"threadId": thread_id, "input": [{
                        "type": "text", "text": "你好。请只回复 OK，不要调用工具，不要修改文件。"}],
                        "approvalPolicy": "never", "clientUserMessageId": turn_id}}) + "\n").encode())
                await proc.stdin.drain()
                completed = False
                async with asyncio.timeout(90):
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            raise RuntimeError("Codex preheat turn stopped")
                        message = json.loads(line)
                        params = message.get("params", {})
                        if message.get("method") == "turn/completed":
                            status = params.get("turn", {}).get("status")
                            if status != "completed":
                                raise RuntimeError("Codex preheat turn did not complete")
                            completed = True
                            break
                if not completed:
                    raise RuntimeError("Codex preheat turn incomplete")
                fresh = await read_limits(self.binary)
                captured = time.time()
                collector.payload = fresh
                collector.updated, collector.failed = captured, False
                collector.codex_archive.record(fresh, captured)
                reset_at = (fresh.get("five_hour") or {}).get("reset_at_epoch")
                self.state = {"thread_id": thread_id, "last_turn_at": captured,
                              "cooldown_until": reset_at if isinstance(reset_at, (int, float))
                              else captured + 18000,
                              "last_result": "completed"}
                self._save()
                return {"ok": True, "status": "completed", "thread_id": thread_id,
                        "reset_at_epoch": reset_at}
            except Exception as exc:
                self.state["last_result"] = "failed"
                self.state["last_error"] = type(exc).__name__
                self._save()
                return {"ok": False, "status": "failed"}
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), 3)


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
        self.preheater = Preheater(binary)

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
                                  "mac_online": collector.performance()["ok"],
                                  "preheat_thread": bool(collector.preheater.state.get("thread_id"))})
    async def preheat(request):
        allowed_ip = os.environ.get("PREHEAT_ALLOWED_IP", "192.168.1.159")
        token_file = os.environ.get("PREHEAT_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else ""
        supplied = request.headers.get("Authorization", "")
        if (token and not secrets.compare_digest(supplied, "Bearer " + token)) or \
                (not token and request.remote != allowed_ip):
            raise web.HTTPUnauthorized(text="Unauthorized")
        return web.json_response(await collector.preheater.run(collector))
    async def preheat_status(request):
        allowed_ip = os.environ.get("PREHEAT_ALLOWED_IP", "192.168.1.159")
        token_file = os.environ.get("PREHEAT_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else ""
        supplied = request.headers.get("Authorization", "")
        if (token and not secrets.compare_digest(supplied, "Bearer " + token)) or \
                (not token and request.remote != allowed_ip):
            raise web.HTTPUnauthorized(text="Unauthorized")
        return web.json_response(collector.preheater.status())
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
    app.router.add_post("/api/preheat", preheat)
    app.router.add_get("/api/preheat/status", preheat_status)
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
