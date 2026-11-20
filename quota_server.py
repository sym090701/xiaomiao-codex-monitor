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
        self.failure_cooldown_seconds = max(60, int(os.environ.get(
            "PREHEAT_FAILURE_COOLDOWN_SECONDS", "1800")))
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
        # Empty means the persisted thread's current model, preserving existing behavior.
        self.preheat_model = os.environ.get("PREHEAT_MODEL", "").strip()
        self.preheat_effort = os.environ.get("PREHEAT_REASONING_EFFORT", "").strip()
        self.preheat_standard_tier = os.environ.get("PREHEAT_STANDARD_TIER", "0") == "1"
        self._prediction_cache = None
        self._prediction_checked = 0
        self._last_decision_key = None

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
        return {"remote_preheat_guard": 1,
                "thread_id_present": bool(self.state.get("thread_id")),
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
                "auto_retry_eligible": self.state.get("auto_retry_eligible", False),
                "last_window_assessment": self.state.get("last_window_assessment"),
                "last_token_usage": self.state.get("last_token_usage"),
                "preheat_model_configured": self.preheat_model or None,
                "decision_log_error": self.state.get("decision_log_error", ""),
                "auto_retry_count": self.state.get("auto_retry_count", 0),
                "failure_cooldown_remaining_seconds": max(0, int(
                    float(self.state.get("failure_cooldown_until", 0) or 0) - time.time())),
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

    @staticmethod
    def _window_assessment(before, after):
        first = (before or {}).get("five_hour") or {}
        second = (after or {}).get("five_hour") or {}
        a, b = first.get("reset_at_epoch"), second.get("reset_at_epoch")
        result = "unknown"
        if type(a) in (int, float) and type(b) in (int, float):
            result = "reset_unchanged" if abs(a - b) <= 120 else "reset_changed_unconfirmed"
        return {"result": result, "before_reset": a, "after_reset": b,
                "before_remaining": first.get("remaining_percent"),
                "after_remaining": second.get("remaining_percent")}

    def _record_decision(self, collector, event, **fields):
        """Append private, credential-free events to the existing NAS sync spool."""
        try:
            root = collector.codex_archive.local_dir or self.state_file.parent
            directory = Path(root) / "preheat-decisions"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            now = time.time()
            date = dt.datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date()
            path = directory / (date.isoformat() + ".ndjson")
            row = {"epoch": now, "event": event, **fields}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=True) + "\n")
            self.state["decision_log_error"] = ""
        except (OSError, TypeError, ValueError) as exc:
            self.state["decision_log_error"] = type(exc).__name__

    def _auto_decision(self, collector, reason):
        self.state["auto_reason"] = reason
        key = (reason, self.state.get("auto_attempted_prediction"),
               (self.state.get("auto_prediction") or {}).get("predicted_at_epoch"))
        if key != self._last_decision_key:
            self._record_decision(collector, "decision", reason=reason,
                                  prediction=self.state.get("auto_prediction"),
                                  due_at=self.state.get("auto_due_at"))
            self._last_decision_key = key
        self._save()

    def _turn_params(self, thread_id, message_id):
        params = {"threadId": thread_id, "input": [{"type": "text", "text":
                  "你好。请只回复 OK，不要调用工具，不要修改文件。"}],
                  "approvalPolicy": "never", "clientUserMessageId": message_id}
        if self.preheat_model:
            params["model"] = self.preheat_model
        if self.preheat_effort:
            params["effort"] = self.preheat_effort
        if self.preheat_standard_tier:
            params["serviceTierForTurn"] = "default"
        return params

    @staticmethod
    def _token_usage(message, thread_id):
        if message.get("method") != "thread/tokenUsage/updated":
            return None
        params = message.get("params") or {}
        if params.get("threadId") != thread_id or not isinstance(params.get("turnId"), str):
            return None
        usage = (params.get("tokenUsage") or {}).get("last") or {}
        fields = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in fields):
            return None
        if usage["cachedInputTokens"] > usage["inputTokens"]:
            return None
        return params["turnId"], {k: usage[k] for k in fields}

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
            failure_until = float(self.state.get("failure_cooldown_until", 0) or 0)
            if now < failure_until:
                return {"ok": False, "status": "failure_cooldown",
                        "retry_after_seconds": int(failure_until - now)}
            if source == "remote":
                # Check under the same lock as manual and automatic preheats.
                usage = collector.snapshot()["usage"]
                window = usage.get("five_hour") or {}
                reset_at, used = window.get("reset_at_epoch"), window.get("used_percent")
                if (not usage.get("ok") or usage.get("stale") or
                        type(reset_at) not in (int, float) or type(used) not in (int, float)):
                    return {"ok": False, "status": "quota_unavailable"}
                if reset_at > now and used > 0:
                    return {"ok": False, "status": "waiting_reset"}
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
            before = copy.deepcopy(collector.payload)
            self.state["preheat_phase"] = "spawn"
            self._save()
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.binary, "app-server", stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
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
                    "params": self._turn_params(thread_id, turn_id)}) + "\n").encode())
                await proc.stdin.drain()
                completed = False
                self.state["preheat_phase"] = "turn_wait"
                self._save()
                by_turn = {}
                completions = {}
                accepted_turn_id = None
                async with asyncio.timeout(self.turn_timeout_seconds):
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            raise RuntimeError("Codex preheat turn stopped")
                        message = json.loads(line)
                        params = message.get("params", {})
                        if message.get("id") == 3:
                            if "error" in message:
                                raise RuntimeError("Codex preheat turn start rejected")
                            accepted_turn_id = (message.get("result", {}).get("turn") or {}).get("id")
                            if not isinstance(accepted_turn_id, str) or not accepted_turn_id:
                                raise RuntimeError("Codex preheat turn ID missing")
                        parsed = self._token_usage(message, thread_id)
                        if parsed:
                            by_turn[parsed[0]] = parsed[1]
                        if message.get("method") == "turn/completed" and params.get("threadId") == thread_id:
                            turn = params.get("turn") or {}
                            if isinstance(turn.get("id"), str):
                                completions[turn["id"]] = turn.get("status")
                        if accepted_turn_id and accepted_turn_id in completions:
                            status = completions[accepted_turn_id]
                            if status != "completed":
                                raise RuntimeError("Codex preheat turn did not complete")
                            completed = True
                            self.state["last_token_usage"] = by_turn.get(accepted_turn_id)
                            break
                if not completed:
                    raise RuntimeError("Codex preheat turn incomplete")
                self.state["turn_completed_at"] = time.time()
                self.state["thread_id"] = thread_id
                self.state["preheat_phase"] = "quota_refresh"
                self._save()
                fresh = await read_limits(self.binary)
                captured = time.time()
                collector.payload = fresh
                collector.updated, collector.failed = captured, False
                collector.codex_archive.record(fresh, captured, record_type="codex_preheat")
                assessment = self._window_assessment(before, fresh)
                self.state["last_window_assessment"] = assessment
                self._record_decision(collector, "turn_completed", source=source,
                                      window=assessment, token_usage=self.state.get("last_token_usage"),
                                      configured_model=self.preheat_model or None,
                                      configured_effort=self.preheat_effort or None)
                reset_at = (fresh.get("five_hour") or {}).get("reset_at_epoch")
                self.state.update({"thread_id": thread_id, "last_turn_at": captured,
                                   "cooldown_until": reset_at if isinstance(reset_at, (int, float))
                                   else captured + 18000,
                                   "last_result": "completed", "last_error": "",
                                   "preheat_phase": "idle", "preheat_last_phase": "completed",
                                   "failure_cooldown_until": 0})
                if source == "auto":
                    self.state.update({"auto_last_turn_at": captured,
                                       "auto_last_result": "completed",
                                       "auto_last_error": "",
                                       "auto_retry_after": 0,
                                       "auto_retry_eligible": False,
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
                self.state["failure_cooldown_until"] = time.time() + self.failure_cooldown_seconds
                if source == "auto":
                    self.state.update({"auto_last_turn_at": time.time(),
                                       "auto_last_result": "failed",
                                       "auto_last_error": error,
                                       "auto_retry_after": time.time() + self.auto_retry_seconds,
                                       "auto_retry_eligible": phase in ("spawn", "initialize", "thread_start", "thread_resume")})
                self._record_decision(collector, "turn_failed", source=source, error=error,
                                      retry_eligible=self.state.get("auto_retry_eligible", False),
                                      window=self._window_assessment(before, None))
                self._save()
                return {"ok": False, "status": "failed", "error": error}
            except Exception as exc:
                phase = self.state.get("preheat_phase", "unknown")
                error = type(exc).__name__ + "@" + phase
                self.state["last_result"] = "failed"
                self.state["last_error"] = error
                self.state["preheat_last_phase"] = phase
                self.state["preheat_phase"] = "idle"
                self.state["failure_cooldown_until"] = time.time() + self.failure_cooldown_seconds
                if source == "auto":
                    self.state.update({"auto_last_turn_at": time.time(),
                                       "auto_last_result": "failed",
                                       "auto_last_error": error,
                                       "auto_retry_after": time.time() + self.auto_retry_seconds,
                                       "auto_retry_eligible": phase in ("spawn", "initialize", "thread_start", "thread_resume")})
                self._record_decision(collector, "turn_failed", source=source, error=error,
                                      retry_eligible=self.state.get("auto_retry_eligible", False),
                                      window=self._window_assessment(before, None))
                self._save()
                return {"ok": False, "status": "failed"}
            finally:
                if proc is not None and proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
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
            self._auto_decision(collector, "quota_snapshot_stale")
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
        lead_seconds = (int(prediction.get("lead_seconds", self.auto_lead_seconds))
                        if prediction.get("lead_basis") == "historical_duration"
                        else self.auto_lead_seconds)
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
            self._auto_decision(collector, "active_window")
            return None
        attempted = self.state.get("auto_attempted_prediction")
        retry_count = int(self.state.get("auto_retry_count", 0) or 0)
        retry_after = float(self.state.get("auto_retry_after", 0) or 0)
        pending = (type(attempted) in (int, float) and now < attempted and
                   self.state.get("auto_last_result") == "failed" and retry_count < 1)
        if pending:
            predicted_at = attempted
        if predicted_at is None:
            self._auto_decision(collector, "insufficient_history")
            return None
        already_attempted = attempted == predicted_at
        if already_attempted and retry_count >= 1:
            self._auto_decision(collector, "prediction_already_attempted")
            return None
        if already_attempted:
            if not self.state.get("auto_retry_eligible", False):
                self._auto_decision(collector, "uncertain_no_retry")
                return None
            if now < retry_after:
                self._auto_decision(collector, "retry_cooldown")
                return None
            if now > retry_after + 600 or now >= predicted_at:
                self._auto_decision(collector, "retry_window_missed")
                return None
        else:
            due_at = self.state["auto_due_at"]
            if now < due_at or now > due_at + 600:
                self._auto_decision(collector, "waiting_for_prediction" if now < due_at
                                    else "prediction_window_missed")
                return None
        if now < float(self.state.get("failure_cooldown_until", 0) or 0):
            self._auto_decision(collector, "failure_cooldown")
            return None
        local_date = dt.datetime.fromtimestamp(
            now, dt.timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if self.state.get("auto_daily_date") != local_date:
            self.state["auto_daily_date"] = local_date
            self.state["auto_daily_count"] = 0
        if (not already_attempted and
                int(self.state.get("auto_daily_count", 0) or 0) >= self.auto_daily_limit):
            self._auto_decision(collector, "daily_limit")
            return None
        if now < float(self.state.get("cooldown_until", 0) or 0):
            self._auto_decision(collector, "cooldown")
            return None
        self.state["auto_attempted_prediction"] = predicted_at
        if already_attempted:
            self.state["auto_retry_count"] = 1
        else:
            self.state["auto_retry_count"] = 0
            self.state["auto_daily_count"] = int(self.state.get("auto_daily_count", 0) or 0) + 1
        self.state["auto_retry_eligible"] = False
        self.state["auto_last_result"] = "running"
        self._auto_decision(collector, "retry_due" if already_attempted else "prediction_due")
        result = await self.run(collector, source="auto")
        if isinstance(result, dict) and result.get("ok") is True:
            self.state["auto_retry_count"] = 1
            self.state["auto_retry_after"] = 0
            self._auto_decision(collector, "completed")
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
    def relay_authorized(request):
        token_file = os.environ.get("PREHEAT_RELAY_TOKEN_FILE")
        if not token_file:
            return False
        try:
            token = Path(token_file).read_text().strip()
        except OSError:
            return False
        return bool(token) and secrets.compare_digest(
            request.headers.get("Authorization", "").encode(), ("Bearer " + token).encode())

    async def preheat(request):
        relay_auth = relay_authorized(request)
        if relay_auth and request.headers.get("X-Preheat-Guard") != "1":
            raise web.HTTPUnauthorized(text="Unauthorized")
        allowed_ip = os.environ.get("PREHEAT_ALLOWED_IP", "192.168.1.159")
        token_file = os.environ.get("PREHEAT_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else ""
        supplied = request.headers.get("Authorization", "")
        legacy_auth = ((token and secrets.compare_digest(supplied, "Bearer " + token)) or
                       (not token and request.remote == allowed_ip))
        if not relay_auth and not legacy_auth:
            raise web.HTTPUnauthorized(text="Unauthorized")
        source = "remote" if request.headers.get("X-Preheat-Guard") == "1" else "manual"
        return web.json_response(await collector.preheater.run(collector, source=source))
    async def preheat_status(request):
        relay_auth = relay_authorized(request)
        allowed_ip = os.environ.get("PREHEAT_ALLOWED_IP", "192.168.1.159")
        token_file = os.environ.get("PREHEAT_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else ""
        supplied = request.headers.get("Authorization", "")
        legacy_auth = ((token and secrets.compare_digest(supplied, "Bearer " + token)) or
                       (not token and request.remote == allowed_ip))
        if not relay_auth and not legacy_auth:
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
