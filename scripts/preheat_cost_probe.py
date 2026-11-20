#!/usr/bin/env python3
"""One-shot, explicitly authorized same-thread compaction and cost probe.

Run as the quota service user only after stopping xiaomiao-quota.service.
This deliberately refuses automatic retries and never prints credentials or messages.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quota_server import Collector, read_limits


async def compact(preheater, thread_id):
    proc = await asyncio.create_subprocess_exec(
        preheater.binary, "app-server", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        await preheater._rpc(proc, 1, "initialize", {"clientInfo": {
            "name": "xiaomiao_preheat_cost_probe", "version": "1"}}, phase=None)
        proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        await proc.stdin.drain()
        await preheater._rpc(proc, 2, "thread/resume", {"threadId": thread_id}, phase=None)
        proc.stdin.write((json.dumps({"id": 3, "method": "thread/compact/start",
                                     "params": {"threadId": thread_id}}) + "\n").encode())
        await proc.stdin.drain()
        compaction_seen = False
        accepted = False
        usage = None
        async with asyncio.timeout(240):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    raise RuntimeError("compact stream ended")
                msg = json.loads(line)
                if msg.get("id") == 3:
                    if "error" in msg:
                        raise RuntimeError("compaction rejected")
                    accepted = True
                    continue
                params = msg.get("params") or {}
                if params.get("threadId") != thread_id:
                    continue
                parsed = preheater._token_usage(msg, thread_id)
                if parsed:
                    usage = parsed[1]
                if (msg.get("method") == "item/completed" and
                        (params.get("item") or {}).get("type") == "contextCompaction"):
                    compaction_seen = True
                if msg.get("method") == "turn/completed":
                    if (params.get("turn") or {}).get("status") != "completed" or not compaction_seen or not accepted:
                        raise RuntimeError("compaction not confirmed")
                    return usage
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


def backup_and_claim(preheater):
    result = subprocess.run(["systemctl", "is-active", "xiaomiao-quota.service"],
                            capture_output=True, text=True, timeout=10)
    if result.stdout.strip() != "inactive":
        raise RuntimeError("quota service must be stopped and inactive")
    state = preheater.state
    thread_id = state.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id or state.get("preheat_phase", "idle") != "idle":
        raise RuntimeError("existing preheat thread must be present and idle")
    now = time.time()
    if any(float(state.get(key, 0) or 0) > now for key in ("cooldown_until", "failure_cooldown_until")):
        raise RuntimeError("preheat is in cooldown")
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    files = list((root / "sessions").rglob("*" + thread_id + "*.jsonl"))
    if len(files) != 1:
        raise RuntimeError("expected exactly one existing thread file")
    dest = preheater.state_file.parent / "preheat-cost-probe"
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    claim = dest / "one-shot-claimed"
    fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write("claimed before compaction; manual review required before retry\n")
    shutil.copy2(files[0], dest / "thread-before.jsonl")
    shutil.copy2(preheater.state_file, dest / "state-before.json")
    os.chmod(dest / "thread-before.jsonl", 0o600)
    os.chmod(dest / "state-before.json", 0o600)
    return thread_id


async def run(binary, model):
    os.environ["PREHEAT_MODEL"] = model
    os.environ["PREHEAT_REASONING_EFFORT"] = "low"
    os.environ["PREHEAT_STANDARD_TIER"] = "1"
    collector = Collector(binary, 60)
    p = collector.preheater
    tid = backup_and_claim(p)
    before = await read_limits(binary)
    if not before.get("ok"):
        raise RuntimeError("quota snapshot unavailable")
    print(json.dumps({"stage": "before", "window": before.get("five_hour")}), flush=True)
    usage = await compact(p, tid)
    after_compact = await read_limits(binary)
    print(json.dumps({"stage": "compacted", "token_usage": usage,
                      "window": after_compact.get("five_hour")}), flush=True)
    collector.payload = after_compact
    collector.updated, collector.failed = time.time(), False
    outcome = await p.run(collector, source="manual")
    print(json.dumps({"stage": "model_probe", "ok": outcome.get("ok"),
                      "status": outcome.get("status"), "error": outcome.get("error"),
                      "configured_model": model, "token_usage": p.state.get("last_token_usage"),
                      "window": p.state.get("last_window_assessment")}), flush=True)
    if not outcome.get("ok"):
        raise RuntimeError("model probe did not complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-one-shot", action="store_true")
    parser.add_argument("--binary", default="/opt/xiaomiao-quota/bin/codex")
    parser.add_argument("--model", default="gpt-6-luna")
    args = parser.parse_args()
    if not args.execute_one_shot:
        parser.error("requires explicit --execute-one-shot")
    try:
        asyncio.run(run(args.binary, args.model))
    except Exception as exc:
        print(json.dumps({"stage": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
