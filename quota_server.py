"""Headless quota collector; speaks the existing XiaoMiao snapshot protocol."""
import argparse
import asyncio
import contextlib
import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import secrets
import time
import uuid
from collections import deque
from zoneinfo import ZoneInfo

from aiohttp import web
from machw_archive import CodexUsageArchive, MacHardwareArchive
from usage_predictor import predict_usage


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
        self.auto_mode = os.environ.get("PREHEAT_AUTO_MODE", "off").strip().lower()
        if self.auto_mode not in ("off", "armed"):
            self.auto_mode = "off"
        self.auto_retry_seconds = max(300, int(os.environ.get(
            "PREHEAT_AUTO_RETRY_SECONDS", "1800")))
        self.auto_daily_limit = max(1, int(os.environ.get(
            "PREHEAT_AUTO_DAILY_LIMIT", "1")))
        self.auto_lead_seconds = max(0, int(os.environ.get(
            "PREHEAT_AUTO_LEAD_SECONDS", "12600")))
        self.auto_history_days = max(7, min(90, int(os.environ.get(
            "PREHEAT_AUTO_HISTORY_DAYS", "28"))))
        self.auto_min_events = max(3, int(os.environ.get(
            "PREHEAT_AUTO_MIN_EVENTS", "3")))
        self.turn_timeout_seconds = max(90, int(os.environ.get(
            "PREHEAT_TURN_TIMEOUT_SECONDS", "180")))
        self._prediction_cache = None
        self._prediction_checked = 0

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
        last_turn_at = self.state.get("last_turn_at")
        last_turn_local = ""
        if isinstance(last_turn_at, (int, float)):
            last_turn_local = dt.datetime.fromtimestamp(
                last_turn_at, dt.timezone.utc).astimezone(
                    ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
        return {"thread_id_present": bool(self.state.get("thread_id")),
                "last_result": self.state.get("last_result"),
                "last_error": self.state.get("last_error"),
                "last_turn_at": self.state.get("last_turn_at"),
                "last_turn_local": last_turn_local,
                "cooldown_active": cooldown_until > time.time(),
                "cooldown_remaining_seconds": max(0, int(cooldown_until - time.time())),
                "auto_mode": self.auto_mode,
                "auto_rate_percent_hour": self.state.get("auto_rate_percent_hour"),
                "auto_last_result": self.state.get("auto_last_result"),
                "auto_last_error": self.state.get("auto_last_error"),
                "auto_last_turn_local": self._local_time(self.state.get("auto_last_turn_at")),
                "auto_reason": self.state.get("auto_reason", ""),
                "auto_lead_hours": round(float(self.state.get(
                    "auto_effective_lead_seconds", self.auto_lead_seconds)) / 3600, 2),
                "auto_lead_basis": self.state.get("auto_lead_basis", "safe_default"),
                "auto_duration_hours": self.state.get("auto_duration_hours"),
                "preheat_phase": self.state.get("preheat_phase", "idle"),
                "preheat_last_phase": self.state.get("preheat_last_phase", ""),
                "auto_retry_count": self.state.get("auto_retry_count", 0),
                "auto_prediction": self.state.get("auto_prediction"),
                "auto_prediction_local": self._local_time(
                    (self.state.get("auto_prediction") or {}).get("predicted_at_epoch")),
                "auto_due_local": self._local_time(self.state.get("auto_due_at"))}

    @staticmethod
    def _local_time(epoch):
        if isinstance(epoch, (int, float)):
            return dt.datetime.fromtimestamp(
                epoch, dt.timezone.utc).astimezone(
                    ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
        return ""

    async def _rpc(self, proc, request_id, method, params, phase=None):
        if phase:
            self.state["preheat_phase"] = phase
            self._save()
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

    async def run(self, collector, source="manual"):
        async with self.lock:
            now = time.time()
            cooldown_until = float(self.state.get("cooldown_until", 0) or 0)
            if now < cooldown_until:
                return {"ok": False, "status": "cooldown",
                        "retry_after_seconds": int(cooldown_until - now)}
            if source == "auto":
                window = collector.payload.get("five_hour") or {}
                reset_at = window.get("reset_at_epoch")
                used = window.get("used_percent")
                if (isinstance(reset_at, (int, float)) and reset_at > now
                        and isinstance(used, (int, float)) and used > 0):
                    return {"ok": False, "status": "waiting_reset"}
            if not collector.payload.get("ok"):
                return {"ok": False, "status": "quota_unavailable"}
            # Persist the attempt before starting a process. Failed/uncertain
            # turns must not produce a duplicate request after a restart.
            self.state.setdefault("preheat_events", []).append(now)
            self.state["preheat_events"] = self.state["preheat_events"][-180:]
            self._save()
            proc = await asyncio.create_subprocess_exec(
                self.binary, "app-server", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                await self._rpc(proc, 1, "initialize", {"clientInfo": {
                    "name": "xiaomiao_preheater", "title": "XiaoMiao quota preheater",
                    "version": "0.1.0"}}, phase="initialize")
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
                    }, phase="thread_start")
                    thread_id = result.get("thread", {}).get("id")
                    if not thread_id:
                        raise RuntimeError("Codex preheat thread was not created")
                else:
                    await self._rpc(proc, 2, "thread/resume", {"threadId": thread_id},
                                    phase="thread_resume")
                turn_id = str(uuid.uuid4())
                self.state["preheat_phase"] = "turn_start"
                self._save()
                proc.stdin.write((json.dumps({"id": 3, "method": "turn/start",
                    "params": {"threadId": thread_id, "input": [{
                        "type": "text", "text": "你好。请只回复 OK，不要调用工具，不要修改文件。"}],
                        "approvalPolicy": "never", "clientUserMessageId": turn_id}}) + "\n").encode())
                await proc.stdin.drain()
                completed = False
                self.state["preheat_phase"] = "turn_wait"
                self._save()
                async with asyncio.timeout(self.turn_timeout_seconds):
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
                self.state["preheat_phase"] = "quota_refresh"
                self._save()
                fresh = await read_limits(self.binary)
                captured = time.time()
                collector.payload = fresh
                collector.updated, collector.failed = captured, False
                collector.codex_archive.record(fresh, captured, record_type="codex_preheat")
                reset_at = (fresh.get("five_hour") or {}).get("reset_at_epoch")
                self.state.update({"thread_id": thread_id, "last_turn_at": captured,
                                   "cooldown_until": reset_at if isinstance(reset_at, (int, float))
                                   else captured + 18000,
                                   "last_result": "completed", "last_error": "",
                                   "preheat_phase": "idle", "preheat_last_phase": "completed"})
                if source == "auto":
                    self.state.update({"auto_last_turn_at": captured,
                                       "auto_last_result": "completed",
                                       "auto_last_error": "",
                                       "auto_retry_after": 0,
                                       "auto_reason": "completed"})
                self._save()
                return {"ok": True, "status": "completed", "thread_id": thread_id,
                        "reset_at_epoch": reset_at}
            except asyncio.TimeoutError:
                phase = self.state.get("preheat_phase", "unknown")
                error = "TimeoutError@" + phase
                self.state["last_result"] = "failed"
                self.state["last_error"] = error
                self.state["preheat_last_phase"] = phase
                self.state["preheat_phase"] = "idle"
                if source == "auto":
                    self.state.update({"auto_last_turn_at": time.time(),
                                       "auto_last_result": "failed",
                                       "auto_last_error": error,
                                       "auto_retry_after": time.time() + self.auto_retry_seconds})
                self._save()
                return {"ok": False, "status": "failed", "error": error}
            except Exception as exc:
                phase = self.state.get("preheat_phase", "unknown")
                error = type(exc).__name__ + "@" + phase
                self.state["last_result"] = "failed"
                self.state["last_error"] = error
                self.state["preheat_last_phase"] = phase
                self.state["preheat_phase"] = "idle"
                if source == "auto":
                    self.state.update({"auto_last_turn_at": time.time(),
                                       "auto_last_result": "failed",
                                       "auto_last_error": error,
                                       "auto_retry_after": time.time() + self.auto_retry_seconds})
                self._save()
                return {"ok": False, "status": "failed"}
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), 3)

    async def maybe_auto(self, collector):
        """Preheat 3.5 hours before a sufficiently supported usage prediction."""
        if self.auto_mode != "armed":
            return None
        now = time.time()
        rate = collector.active_rate_percent_hour()
        self.state["auto_rate_percent_hour"] = round(rate, 3)
        self.state["auto_reason"] = ""
        if (not collector.payload.get("ok") or collector.failed or collector.updated is None
                or now - collector.updated > collector.interval * 3):
            self.state["auto_reason"] = "quota_snapshot_stale"
            self._save()
            return None
        if self._prediction_cache is None or time.monotonic() - self._prediction_checked >= 300:
            excluded = self.state.get("preheat_events", []) + [
                self.state.get("last_turn_at"), self.state.get("auto_last_turn_at")]
            self._prediction_cache = await asyncio.to_thread(
                predict_usage, collector.codex_archive.local_dir, now,
                self.auto_history_days, self.auto_min_events, excluded)
            self._prediction_checked = time.monotonic()
        prediction = self._prediction_cache
        self.state["auto_prediction"] = prediction
        predicted_at = prediction.get("predicted_at_epoch")
        lead_seconds = int(prediction.get("lead_seconds", self.auto_lead_seconds))
        self.state["auto_effective_lead_seconds"] = lead_seconds
        self.state["auto_lead_basis"] = prediction.get("lead_basis", "safe_default")
        self.state["auto_duration_hours"] = prediction.get("duration_hours")
        self.state["auto_due_at"] = (predicted_at - lead_seconds
                                     if predicted_at is not None else None)
        window = collector.payload.get("five_hour") or {}
        reset_at = window.get("reset_at_epoch")
        used = window.get("used_percent")
        if (isinstance(reset_at, (int, float)) and reset_at > now
                and isinstance(used, (int, float)) and used > 0):
            self.state["auto_reason"] = "active_window"
            self._save()
            return None
        if predicted_at is None:
            self.state["auto_reason"] = "insufficient_history"
            self._save()
            return None
        due_at = self.state["auto_due_at"]
        if now < due_at or now > due_at + 600:
            self.state["auto_reason"] = ("waiting_for_prediction" if now < due_at
                                         else "prediction_window_missed")
            self._save()
            return None
        already_attempted = self.state.get("auto_attempted_prediction") == predicted_at
        retry_count = int(self.state.get("auto_retry_count", 0) or 0)
        if already_attempted and retry_count >= 1:
            self.state["auto_reason"] = "prediction_already_attempted"
            self._save()
            return None
        local_date = dt.datetime.fromtimestamp(
            now, dt.timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if self.state.get("auto_daily_date") != local_date:
            self.state["auto_daily_date"] = local_date
            self.state["auto_daily_count"] = 0
        if (not already_attempted and
                int(self.state.get("auto_daily_count", 0) or 0) >= self.auto_daily_limit):
            self.state["auto_reason"] = "daily_limit"
            self._save()
            return None
        retry_after = float(self.state.get("auto_retry_after", 0) or 0)
        if now < retry_after:
            self.state["auto_reason"] = "retry_cooldown"
            return None
        if now < float(self.state.get("cooldown_until", 0) or 0):
            self.state["auto_reason"] = "cooldown"
            self._save()
            return None
        self.state["auto_reason"] = "retry_due" if already_attempted else "prediction_due"
        self.state["auto_attempted_prediction"] = predicted_at
        if already_attempted:
            self.state["auto_retry_count"] = 1
        else:
            self.state["auto_retry_count"] = 0
            self.state["auto_daily_count"] = int(self.state.get("auto_daily_count", 0) or 0) + 1
        self._save()
        result = await self.run(collector, source="auto")
        if isinstance(result, dict) and result.get("ok") is True:
            self.state["auto_retry_count"] = 1
            self.state["auto_retry_after"] = 0
            self.state["auto_reason"] = "completed"
            self._save()
        return result


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
        self._usage_samples = deque(maxlen=240)
        self._active_rate = 0.0

    def record_usage_sample(self, usage, captured):
        window = usage.get("five_hour") or {}
        remaining = window.get("remaining_percent")
        if isinstance(remaining, (int, float)) and math.isfinite(remaining):
            self._usage_samples.append((captured, float(remaining)))
            if len(self._usage_samples) >= 2:
                previous_at, previous_remaining = self._usage_samples[-2]
                delta_seconds = captured - previous_at
                drop = previous_remaining - float(remaining)
                # Ignore reset jumps and flat data. The falling segment is
                # diagnostic only; scheduling uses archived usage habits.
                if 30 <= delta_seconds <= 900 and drop > 0.05:
                    instant_rate = drop * 3600.0 / delta_seconds
                    self._active_rate = (instant_rate if not self._active_rate else
                                         0.65 * self._active_rate + 0.35 * instant_rate)

    def active_rate_percent_hour(self):
        return max(0.0, float(self._active_rate))

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
                self.record_usage_sample(fresh, captured)
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

    async def auto_preheat_loop():
        # Run independently of HTTP requests and the device. The Ubuntu
        # service remains the source of truth while XiaoMiao is asleep.
        while True:
            try:
                if collector.preheater.auto_mode == "armed":
                    await collector.preheater.maybe_auto(collector)
            except (OSError, RuntimeError, ValueError, KeyError,
                    asyncio.TimeoutError):
                # Polling must continue even if a preheat attempt fails.
                pass
            await asyncio.sleep(30)

    async def lifetime(_app):
        task = asyncio.create_task(collector.poll())
        auto_task = asyncio.create_task(auto_preheat_loop())
        sync_tasks = []
        if collector.archive.enabled:
            sync_tasks.append(asyncio.create_task(collector.archive.sync_loop()))
        if collector.codex_archive.enabled:
            sync_tasks.append(asyncio.create_task(collector.codex_archive.sync_loop()))
        yield
        task.cancel()
        auto_task.cancel()
        for sync_task in sync_tasks:
            sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await auto_task
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
