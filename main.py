#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
import datetime
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from pathlib import Path

from aiohttp import WSMsgType, web
import requests
import psutil

from screen_state import ScreenState


DEFAULT_URL = "https://chatgpt.com/backend-api/codex/usage"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
USAGE_REFRESH_SECONDS = 15
PUSH_INTERVAL_SECONDS = 5
CODEX_WATCH_INTERVAL_SECONDS = 0.25
WEB_ROOT = Path(__file__).resolve().parent / "web"
DEVICE_CONFIG_FILE = Path(__file__).resolve().parent / ".codex-display-config.json"
CODEX_HOOK_STATES = {
    "UserPromptSubmit": "working",
    "PermissionRequest": "permission",
    "PostToolUse": "working",
    "Stop": "stopped",
}


def memory_pressure_percent(memory):
    if sys.platform == "darwin" and shutil.which("memory_pressure"):
        try:
            output = subprocess.check_output(
                ["memory_pressure", "-Q"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            marker = "System-wide memory free percentage:"
            free_percent = float(output.split(marker, 1)[1].split("%", 1)[0].strip())
            return max(0.0, min(100.0, 100.0 - free_percent))
        except (IndexError, ValueError, subprocess.SubprocessError):
            pass

    return max(0.0, min(100.0, 100.0 - memory.available * 100.0 / memory.total))


class PerformanceCollector:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_net = psutil.net_io_counters()
        self.last_net_time = time.monotonic()

    def collect(self):
        with self.lock:
            now = time.monotonic()
            net = psutil.net_io_counters()
            elapsed = max(now - self.last_net_time, 0.001)
            rx_mbps = (net.bytes_recv - self.last_net.bytes_recv) * 8 / elapsed / 1_000_000
            tx_mbps = (net.bytes_sent - self.last_net.bytes_sent) * 8 / elapsed / 1_000_000
            self.last_net = net
            self.last_net_time = now

            memory = self._memory_stats()
            disk_path = "/System/Volumes/Data" if Path("/System/Volumes/Data").is_dir() else "/"
            disk = psutil.disk_usage(disk_path)
            load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0.0, 0.0, 0.0)
            boot_time = psutil.boot_time()
            cpu_samples = psutil.cpu_percent(interval=0.1, percpu=True)
            cpu_percent = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0

            return {
                "ok": True,
                "current_system_time": current_system_time(),
                "host_name": socket.gethostname(),
                "cpu_percent": round(cpu_percent, 1),
                "cpu_count": psutil.cpu_count() or 1,
                "memory_percent": round(memory.percent, 1),
                "memory_pressure_percent": round(memory_pressure_percent(memory), 1),
                "memory_used_gb": round(memory.used / (1024 ** 3), 2),
                "memory_total_gb": round(memory.total / (1024 ** 3), 2),
                "disk_percent": round(disk.percent, 1),
                "disk_path": disk_path,
                "disk_used_gb": round(disk.used / (1024 ** 3), 1),
                "disk_total_gb": round(disk.total / (1024 ** 3), 1),
                "load_1": round(load[0], 2),
                "load_5": round(load[1], 2),
                "load_15": round(load[2], 2),
                "net_rx_mbps": round(max(rx_mbps, 0.0), 2),
                "net_tx_mbps": round(max(tx_mbps, 0.0), 2),
                "uptime_seconds": max(0, int(time.time() - boot_time)),
            }

    @staticmethod
    def _memory_stats():
        if sys.platform == "darwin":
            vm_output = subprocess.check_output(
                ["vm_stat"], text=True, stderr=subprocess.DEVNULL, timeout=2
            )
            lines = vm_output.splitlines()
            page_size = int(lines[0].split("page size of ", 1)[1].split(" bytes", 1)[0])
            pages = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                pages[key.strip().strip('"')] = int(value.strip().rstrip("."))

            total = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip())
            available_pages = sum(pages.get(key, 0) for key in (
                "Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"
            ))
            available = min(total, available_pages * page_size)
            used = max(0, total - available)
            return SimpleNamespace(
                total=total,
                available=available,
                used=used,
                percent=used * 100.0 / total,
            )

        error = None
        for _ in range(3):
            try:
                return psutil.virtual_memory()
            except (OSError, RuntimeError) as exc:
                error = exc
                time.sleep(0.02)
        raise error


performance_collector = PerformanceCollector()
screen_state = ScreenState(
    lambda screen_on: print(
        f"computer screen {'ON' if screen_on else 'OFF'}", flush=True
    )
)


def current_system_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_token():
    auth_file = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"

    if not auth_file.exists():
        raise RuntimeError(f"找不到 auth.json: {auth_file}")

    with auth_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    token = (
        data.get("tokens", {}).get("access_token")
        or data.get("access_token")
        or data.get("oauth", {}).get("access_token")
    )

    if not token:
        raise RuntimeError(f"无法从 {auth_file} 读取 access_token")

    return token


def format_ts(ts):
    if ts is None:
        return None

    ts = int(ts)

    # 兼容毫秒时间戳
    if ts > 10_000_000_000:
        ts = ts / 1000

    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds):
    if seconds is None:
        return None

    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h}小时{m}分钟{s}秒"
    if m > 0:
        return f"{m}分钟{s}秒"
    return f"{s}秒"


def parse_window(w):
    if not w:
        return {
            "used_percent": None,
            "remaining_percent": None,
            "reset_at": None,
            "reset_after": None,
            "limit_window_seconds": None,
        }

    used = w.get("used_percent")
    remaining = None

    if used is not None:
        remaining = max(0, min(100, 100 - float(used)))

    return {
        "used_percent": used,
        "remaining_percent": remaining,
        "reset_at": format_ts(w.get("reset_at")),
        "reset_after": format_duration(w.get("reset_after_seconds")),
        "reset_after_seconds": w.get("reset_after_seconds"),
        "limit_window_seconds": w.get("limit_window_seconds"),
    }


def classify_rate_limit_windows(*windows):
    classified = {
        "five_hour": parse_window(None),
        "weekly": parse_window(None),
    }
    remaining = []

    for window in windows:
        if not window:
            continue
        parsed = parse_window(window)
        duration = parsed.get("limit_window_seconds")
        if duration == 5 * 60 * 60:
            classified["five_hour"] = parsed
        elif duration == 7 * 24 * 60 * 60:
            classified["weekly"] = parsed
        else:
            remaining.append(parsed)

    # Preserve compatibility with responses that omit window durations.
    for name in ("five_hour", "weekly"):
        if classified[name]["used_percent"] is None and remaining:
            classified[name] = remaining.pop(0)

    return classified


def parse_usage(data):
    # 你实际拿到的新结构
    if "rate_limit" in data:
        rl = data["rate_limit"]
        windows = classify_rate_limit_windows(
            rl.get("primary_window"),
            rl.get("secondary_window"),
        )

        return {
            "ok": True,
            "current_system_time": current_system_time(),
            "user_id": data.get("user_id"),
            "account_id": data.get("account_id"),
            "email": data.get("email"),
            "plan_type": data.get("plan_type"),
            "allowed": rl.get("allowed"),
            "limit_reached": rl.get("limit_reached"),
            "five_hour": windows["five_hour"],
            "weekly": windows["weekly"],
            "credits": data.get("credits"),
            "rate_limit_reset_credits": data.get("rate_limit_reset_credits"),
        }

    # 兼容另一种社区里见过的结构
    if "rateLimits" in data:
        rl = data["rateLimits"]

        primary = rl.get("primary", {}) or {}
        secondary = rl.get("secondary", {}) or {}

        def convert_old_window(w):
            used = w.get("used_percent") or w.get("usedPercent")
            reset = w.get("resets_at") or w.get("resetsAt")
            window_minutes = w.get("window_minutes") or w.get("windowDurationMins")

            return {
                "used_percent": used,
                "remaining_percent": None if used is None else max(0, min(100, 100 - float(used))),
                "reset_at": format_ts(reset),
                "reset_after": None,
                "reset_after_seconds": None,
                "limit_window_seconds": None if window_minutes is None else int(window_minutes) * 60,
            }

        return {
            "ok": True,
            "current_system_time": current_system_time(),
            "plan_type": data.get("plan_type") or data.get("planType"),
            "five_hour": convert_old_window(primary),
            "weekly": convert_old_window(secondary),
            "credits": data.get("credits") or rl.get("credits"),
        }

    return {
        "ok": False,
        "current_system_time": current_system_time(),
        "error": "响应里没找到 rate_limit 或 rateLimits",
        "raw": data,
    }


def request_proxies(url):
    proxy = os.environ.get("CODEX_USAGE_PROXY")
    if proxy:
        if proxy.lower() in ("none", "off", "false", "0", "direct"):
            return {}
        return {
            "http": proxy,
            "https": proxy,
        }

    system_proxy = detect_system_proxy()
    if not system_proxy:
        return None

    return {
        "http": system_proxy,
        "https": system_proxy,
    }


def detect_system_proxy():
    if not shutil.which("networksetup"):
        return None

    for service in network_services():
        for proxy_type in ("secure", "web", "socks"):
            proxy = service_proxy(service, proxy_type)
            if proxy:
                return proxy

    return None


def network_services():
    try:
        output = subprocess.check_output(
            ["networksetup", "-listallnetworkservices"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    services = []
    for line in output.splitlines():
        service = line.strip()
        if not service or service.startswith("An asterisk") or service.startswith("*"):
            continue
        services.append(service)
    return services


def service_proxy(service, proxy_type):
    commands = {
        "secure": ("-getsecurewebproxy", "http"),
        "web": ("-getwebproxy", "http"),
        "socks": ("-getsocksfirewallproxy", "socks5"),
    }
    command, scheme = commands[proxy_type]

    try:
        output = subprocess.check_output(
            ["networksetup", command, service],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    fields = parse_networksetup_fields(output)
    if fields.get("Enabled", "").lower() != "yes":
        return None

    server = fields.get("Server")
    port = fields.get("Port")
    if not server or not port or port == "0":
        return None

    return f"{scheme}://{server}:{port}"


def parse_networksetup_fields(output):
    fields = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def request_usage(token, url):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-usage-local/0.2",
    }

    resp = requests.get(url, headers=headers, timeout=20, proxies=request_proxies(url))

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:1000]}")

    content_type = resp.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise RuntimeError(
            f"返回的不是 JSON，content-type={content_type}\n"
            f"前 500 字符：\n{resp.text[:500]}"
        )

    return resp.json()


def print_human(parsed):
    print(f"当前系统时间: {parsed.get('current_system_time')}")
    print(f"计划: {parsed.get('plan_type')}")
    print(f"允许使用: {parsed.get('allowed')}")
    print(f"是否触发限制: {parsed.get('limit_reached')}")
    print()

    five = parsed["five_hour"]
    week = parsed["weekly"]

    print(f"5小时已用: {five['used_percent']}%")
    print(f"5小时剩余: {five['remaining_percent']}%")
    print(f"5小时重置: {five['reset_at']}")
    print(f"5小时距离重置: {five['reset_after']}")
    print()

    print(f"周已用: {week['used_percent']}%")
    print(f"周剩余: {week['remaining_percent']}%")
    print(f"周重置: {week['reset_at']}")
    print(f"周距离重置: {week['reset_after']}")

    reset_credits = parsed.get("rate_limit_reset_credits")
    if reset_credits:
        print()
        print(f"可用重置次数: {reset_credits.get('available_count')}")


def build_usage_payload(url):
    token = load_token()
    data = request_usage(token, url)
    return parse_usage(data)


class UsageCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.payload = None
        self.updated_at = None
        self.last_error = None

    def refresh(self, url):
        try:
            payload = build_usage_payload(url)
            with self.lock:
                self.payload = payload
                self.updated_at = current_system_time()
                self.last_error = None
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
                if self.payload is None:
                    self.payload = {
                        "ok": False,
                        "current_system_time": current_system_time(),
                        "error": str(exc),
                    }
                    self.updated_at = current_system_time()

    def snapshot(self):
        with self.lock:
            payload = dict(self.payload) if self.payload is not None else {
                "ok": False,
                "current_system_time": current_system_time(),
                "error": "usage data warming up",
            }
            payload["server_updated_at"] = self.updated_at
            if self.last_error:
                payload["refresh_error"] = self.last_error
            return payload


usage_cache = UsageCache()


class PushService:
    def __init__(self, usage_url):
        self.usage_url = usage_url
        self.clients = set()
        self.devices = {}
        self.device_sockets = {}
        self.performance = {
            "ok": False,
            "current_system_time": current_system_time(),
            "error": "performance data warming up",
        }
        self.desired_config = self._load_desired_config()
        self.codex_sessions = {}
        self.codex_status = {
            "state": "stopped",
            "event": "Stop",
            "active_conversations": 0,
            "updated_at": current_system_time(),
        }
        self.loop = None

    @staticmethod
    def _load_desired_config():
        try:
            return json.loads(DEVICE_CONFIG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def save_desired_config(self):
        temporary = DEVICE_CONFIG_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.desired_config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(DEVICE_CONFIG_FILE)

    def _rebuild_codex_status(self, session_id="legacy", updated_at=None):
        updated_at = updated_at or current_system_time()
        waiting = [
            (key, value) for key, value in self.codex_sessions.items()
            if value["state"] == "permission"
        ]
        candidates = waiting or list(self.codex_sessions.items())
        if candidates:
            selected_id, selected = max(
                candidates, key=lambda item: item[1]["updated_at"]
            )
            self.codex_status = {
                key: value for key, value in selected.items()
                if key not in ("transcript_path", "transcript_offset")
            }
            self.codex_status.update({
                "session_id": selected_id,
                "active_conversations": len(self.codex_sessions),
            })
        else:
            self.codex_status = {
                "state": "stopped",
                "event": "Stop",
                "session_id": session_id,
                "active_conversations": 0,
                "updated_at": updated_at,
            }
        return self.codex_status

    def update_codex_status(self, event, session_id, transcript_path=None):
        session_id = str(session_id or "legacy")
        state = CODEX_HOOK_STATES[event]
        updated_at = current_system_time()
        if state == "stopped":
            self.codex_sessions.pop(session_id, None)
        else:
            previous = self.codex_sessions.get(session_id, {})
            path = transcript_path or previous.get("transcript_path")
            transcript_offset = previous.get("transcript_offset", 0)
            if transcript_path:
                try:
                    transcript_offset = Path(transcript_path).stat().st_size
                except OSError:
                    transcript_offset = 0
            self.codex_sessions[session_id] = {
                "state": state,
                "event": event,
                "updated_at": updated_at,
                "transcript_path": path,
                "transcript_offset": transcript_offset,
            }
        return self._rebuild_codex_status(session_id, updated_at)

    def reconcile_codex_sessions(self):
        completed_sessions = []
        for session_id, session in tuple(self.codex_sessions.items()):
            transcript_path = session.get("transcript_path")
            if not transcript_path:
                continue
            try:
                with Path(transcript_path).open("rb") as transcript:
                    transcript.seek(session.get("transcript_offset", 0))
                    for raw_line in transcript:
                        try:
                            record = json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if record.get("type") != "event_msg":
                            continue
                        event_type = record.get("payload", {}).get("type")
                        if event_type in ("turn_aborted", "task_complete"):
                            completed_sessions.append(session_id)
                            break
                    session["transcript_offset"] = transcript.tell()
            except OSError:
                continue

        if not completed_sessions:
            return False
        for session_id in completed_sessions:
            self.codex_sessions.pop(session_id, None)
        self._rebuild_codex_status(completed_sessions[-1])
        return True

    def snapshot(self):
        usage = usage_cache.snapshot()
        usage = {key: usage.get(key) for key in (
            "ok", "current_system_time", "plan_type", "limit_reached",
            "five_hour", "weekly", "error", "refresh_error", "server_updated_at",
        ) if key in usage}
        return {
            "type": "snapshot",
            "sent_at": current_system_time(),
            "push_interval_seconds": PUSH_INTERVAL_SECONDS,
            "screen_on": screen_state.snapshot()["screen_on"],
            "usage": usage,
            "performance": self.performance,
            "codex": self.codex_status,
        }

    async def broadcast(self, payload):
        if not self.clients:
            return
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        stale = []
        for ws in tuple(self.clients):
            try:
                await ws.send_str(message)
            except (ConnectionError, RuntimeError):
                stale.append(ws)
        for ws in stale:
            self.clients.discard(ws)

    async def push_screen(self, screen_on):
        await self.broadcast({
            "type": "screen",
            "screen_on": bool(screen_on),
            "sent_at": current_system_time(),
        })

    def screen_changed(self, screen_on):
        print(f"computer screen {'ON' if screen_on else 'OFF'}", flush=True)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.push_screen(screen_on), self.loop)

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=20, receive_timeout=45)
        await ws.prepare(request)
        self.clients.add(ws)
        device_id = request.query.get("device", request.remote or "esp32")
        previous = self.device_sockets.get(device_id)
        if previous and previous is not ws and not previous.closed:
            await previous.close(code=1001, message=b"device reconnected")
        self.device_sockets[device_id] = ws
        self.devices[device_id] = {
            "connected": True,
            "remote": request.remote,
            "last_seen": current_system_time(),
        }
        await ws.send_json(self.snapshot())
        if self.desired_config:
            await ws.send_json({"type": "config", "config": self.desired_config})

        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "error": "invalid json"})
                    continue
                device = self.devices.setdefault(device_id, {})
                device.update({
                    "connected": True,
                    "remote": request.remote,
                    "last_seen": current_system_time(),
                    "message": payload,
                })
                if payload.get("type") in ("hello", "status", "config_ack"):
                    device["config"] = payload.get("config", device.get("config", {}))
        finally:
            self.clients.discard(ws)
            if self.device_sockets.get(device_id) is ws:
                self.device_sockets.pop(device_id, None)
                device = self.devices.setdefault(device_id, {})
                device["connected"] = False
                device["last_seen"] = current_system_time()
        return ws

    async def usage_refresh_loop(self):
        while True:
            await asyncio.to_thread(usage_cache.refresh, self.usage_url)
            await asyncio.sleep(USAGE_REFRESH_SECONDS)

    async def push_loop(self):
        while True:
            try:
                self.performance = await asyncio.to_thread(performance_collector.collect)
            except Exception as exc:
                self.performance = {
                    "ok": False,
                    "current_system_time": current_system_time(),
                    "error": str(exc),
                }
            await self.broadcast(self.snapshot())
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)

    async def codex_watch_loop(self):
        while True:
            if self.reconcile_codex_sessions():
                await self.broadcast({
                    "type": "codex_status",
                    "codex": self.codex_status,
                })
            await asyncio.sleep(CODEX_WATCH_INTERVAL_SECONDS)


async def index_handler(_request):
    return web.FileResponse(WEB_ROOT / "index.html")


async def api_state(request):
    service = request.app["push_service"]
    snapshot = service.snapshot()
    return web.json_response({
        "ok": True,
        "screen": screen_state.snapshot(),
        "usage": snapshot["usage"],
        "performance": service.performance,
        "codex": service.codex_status,
        "devices": service.devices,
        "desired_config": service.desired_config,
        "push_interval_seconds": PUSH_INTERVAL_SECONDS,
        "usage_refresh_seconds": USAGE_REFRESH_SECONDS,
    })


async def api_device_config(request):
    service = request.app["push_service"]
    if request.method == "GET":
        return web.json_response({"ok": True, "config": service.desired_config})

    try:
        config = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(text="invalid json")
    allowed = {
        "display_mode", "memory_metric", "screen_flipped", "follow_host_screen",
        "backlight_brightness",
        "performance_background", "performance_cpu_color", "performance_memory_color",
        "performance_disk_color", "performance_load_color", "performance_rx_color",
        "performance_tx_color", "codex_permission_color", "codex_working_color",
        "codex_stop_color", "codex_working_animation",
    }
    service.desired_config = {key: value for key, value in config.items() if key in allowed}
    await asyncio.to_thread(service.save_desired_config)
    await service.broadcast({"type": "config", "config": service.desired_config})
    return web.json_response({
        "ok": True,
        "config": service.desired_config,
        "connected_devices": len(service.clients),
    })


async def api_codex_hook(request):
    service = request.app["push_service"]
    try:
        payload = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(text="invalid json")
    event = payload.get("event")
    if event not in CODEX_HOOK_STATES:
        raise web.HTTPBadRequest(text="unsupported hook event")
    service.update_codex_status(
        event, payload.get("session_id"), payload.get("transcript_path")
    )
    await service.broadcast({"type": "codex_status", "codex": service.codex_status})
    return web.json_response({"ok": True, "codex": service.codex_status})


async def legacy_usage(_request):
    payload = usage_cache.snapshot()
    return web.json_response(payload, status=200 if payload.get("ok") else 503)


async def legacy_performance(request):
    return web.json_response(request.app["push_service"].performance)


async def legacy_screen(_request):
    return web.json_response(screen_state.snapshot())


async def start_background_tasks(app):
    service = app["push_service"]
    service.loop = asyncio.get_running_loop()
    screen_state.set_on_change(service.screen_changed)
    screen_state.start()
    app["usage_task"] = asyncio.create_task(service.usage_refresh_loop())
    app["push_task"] = asyncio.create_task(service.push_loop())
    app["codex_watch_task"] = asyncio.create_task(service.codex_watch_loop())


async def stop_background_tasks(app):
    for name in ("usage_task", "push_task", "codex_watch_task"):
        app[name].cancel()
    await asyncio.gather(
        app["usage_task"], app["push_task"], app["codex_watch_task"],
        return_exceptions=True,
    )


def serve(url, host=None, port=None):
    host = host or os.environ.get("CODEX_USAGE_HOST", DEFAULT_HOST)
    port = port or int(os.environ.get("CODEX_USAGE_PORT", DEFAULT_PORT))
    service = PushService(url)
    app = web.Application()
    app["push_service"] = service
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", service.websocket)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/device/config", api_device_config)
    app.router.add_post("/api/device/config", api_device_config)
    app.router.add_post("/api/codex/hook", api_codex_hook)
    app.router.add_get("/usage", legacy_usage)
    app.router.add_get("/performance", legacy_performance)
    app.router.add_get("/screen", legacy_screen)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(stop_background_tasks)
    print(f"codex display service listening on http://{host}:{port}")
    print("dashboard: /")
    print("websocket: /ws")
    web.run_app(app, host=host, port=port, print=None)


def main():
    parser = argparse.ArgumentParser(description="Fetch or serve Codex usage information.")
    parser.add_argument("--serve", action="store_true", help="serve parsed usage JSON over HTTP")
    parser.add_argument("--raw", action="store_true", help="print raw API JSON")
    parser.add_argument("--json", action="store_true", help="print parsed JSON")
    parser.add_argument("--url", default=os.environ.get("CODEX_USAGE_URL", DEFAULT_URL))
    parser.add_argument("--host", default=None, help="HTTP host for --serve, e.g. 0.0.0.0")
    parser.add_argument("--port", type=int, default=None, help="HTTP port for --serve")
    args = parser.parse_args()
    url = args.url

    try:
        if args.serve:
            serve(url, args.host, args.port)
            return

        if args.raw:
            token = load_token()
            data = request_usage(token, url)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return

        parsed = build_usage_payload(url)

        if args.json:
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
            return

        if not parsed.get("ok"):
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
            sys.exit(1)

        print_human(parsed)

    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
