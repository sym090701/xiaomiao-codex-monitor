#!/usr/bin/env python3
"""Install, remove, or deliver Codex lifecycle events to the local display service."""

import argparse
import json
import os
import shlex
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_FILE = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
ENDPOINT_FILE = PROJECT_ROOT / ".codex-hook-config.json"
HOOK_EVENTS = ("PermissionRequest", "PostToolUse", "UserPromptSubmit", "Stop")
HOOK_COMMAND = (
    f"{shlex.quote(sys.executable)} "
    f"{shlex.quote(str(Path(__file__).resolve()))} --notify"
)
SESSION_ID_FIELDS = (
    "session_id", "conversation_id", "thread_id",
    "sessionId", "conversationId", "threadId",
)


def read_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def is_our_handler(handler):
    command = handler.get("command", "") if isinstance(handler, dict) else ""
    return str(Path(__file__).resolve()) in command and "--notify" in command


def installed():
    try:
        document = read_json(HOOKS_FILE, {})
    except (OSError, json.JSONDecodeError):
        return False
    hooks = document.get("hooks", {})
    return all(
        any(
            is_our_handler(handler)
            for group in hooks.get(event, [])
            if isinstance(group, dict)
            for handler in group.get("hooks", [])
        )
        for event in HOOK_EVENTS
    )


def install():
    try:
        document = read_json(HOOKS_FILE, {})
    except json.JSONDecodeError as exc:
        raise SystemExit(f"无法安装：{HOOKS_FILE} 不是有效 JSON：{exc}")
    if not isinstance(document, dict):
        raise SystemExit(f"无法安装：{HOOKS_FILE} 顶层必须是 JSON 对象")

    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"无法安装：{HOOKS_FILE} 的 hooks 必须是 JSON 对象")

    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SystemExit(f"无法安装：hooks.{event} 必须是数组")
        if any(
            is_our_handler(handler)
            for group in groups
            if isinstance(group, dict)
            for handler in group.get("hooks", [])
        ):
            continue
        groups.append({
            "hooks": [{
                "type": "command",
                "command": HOOK_COMMAND,
                "timeout": 3,
                "statusMessage": "Updating ESP32 Codex status",
            }]
        })

    atomic_write_json(HOOKS_FILE, document)
    print(f"Codex Hook 已安装：{HOOKS_FILE}")
    print("请在 Codex 中运行 /hooks，审阅并信任新增的四条 Hook。")


def uninstall():
    if not HOOKS_FILE.exists():
        print("Codex Hook 未安装")
        return
    try:
        document = read_json(HOOKS_FILE, {})
    except json.JSONDecodeError as exc:
        raise SystemExit(f"无法卸载：{HOOKS_FILE} 不是有效 JSON：{exc}")

    hooks = document.get("hooks", {})
    removed = 0
    if isinstance(hooks, dict):
        for event in HOOK_EVENTS:
            kept_groups = []
            for group in hooks.get(event, []):
                if not isinstance(group, dict):
                    kept_groups.append(group)
                    continue
                handlers = group.get("hooks", [])
                kept_handlers = [handler for handler in handlers if not is_our_handler(handler)]
                removed += len(handlers) - len(kept_handlers)
                if kept_handlers:
                    updated = dict(group)
                    updated["hooks"] = kept_handlers
                    kept_groups.append(updated)
            if kept_groups:
                hooks[event] = kept_groups
            else:
                hooks.pop(event, None)
        if not hooks:
            document.pop("hooks", None)

    atomic_write_json(HOOKS_FILE, document)
    print(f"已卸载 {removed} 条 ESP32 Codex Hook；其他 Hook 保持不变")


def configure(host, port):
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    atomic_write_json(ENDPOINT_FILE, {"url": f"http://{host}:{port}/api/codex/hook"})


def session_id_from_payload(payload):
    for key in SESSION_ID_FIELDS:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    # transcript_path is unique per Codex session on versions that do not expose an ID.
    transcript_path = payload.get("transcript_path")
    if transcript_path:
        return f"transcript:{transcript_path}"
    return "legacy"


def notify():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    event = payload.get("hook_event_name")
    if event not in HOOK_EVENTS:
        return
    try:
        endpoint = read_json(ENDPOINT_FILE, {}).get(
            "url", "http://127.0.0.1:8080/api/codex/hook"
        )
        body = json.dumps({
            "event": event,
            "session_id": session_id_from_payload(payload),
            "transcript_path": payload.get("transcript_path"),
        }).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=0.75):
            pass
    except (OSError, ValueError, urllib.error.URLError):
        # The display service is optional; never interrupt a Codex turn.
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.install:
        install()
    elif args.uninstall:
        uninstall()
    elif args.status:
        raise SystemExit(0 if installed() else 1)
    elif args.configure:
        configure(args.host, args.port)
    else:
        notify()


if __name__ == "__main__":
    main()
