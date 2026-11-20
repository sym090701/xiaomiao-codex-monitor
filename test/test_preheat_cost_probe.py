import asyncio
import contextlib
import io
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from quota_server import Preheater
from scripts.preheat_cost_probe import compact, run


class FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, value):
        self.writes.append(value)

    async def drain(self):
        pass


class FakeStdout:
    def __init__(self, messages):
        self.messages = iter(messages)

    async def readline(self):
        return next(self.messages, b"")


class FakeProcess:
    def __init__(self, messages):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout([json.dumps(m).encode() + b"\n" for m in messages])
        self.returncode = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class CompactProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_checks_window_then_tests_model_before_compaction(self):
        events = []
        p = SimpleNamespace(state={}, _save=Mock(), run=AsyncMock())
        async def fake_run(*_args, **_kwargs):
            events.append("model")
            return {"ok": True, "status": "completed"}
        async def fake_compact(*_args):
            events.append("compact")
            return None
        p.run.side_effect = fake_run
        c = SimpleNamespace(preheater=p, payload=None, updated=None, failed=False)
        usage = {"ok": True, "five_hour": {"used_percent": 0, "reset_at_epoch": 1}}
        with patch.dict(os.environ, {}, clear=False), \
             patch("scripts.preheat_cost_probe.Collector", return_value=c), \
             patch("scripts.preheat_cost_probe.backup_and_claim", return_value=("thread", "gpt-6.1-sol")), \
             patch("scripts.preheat_cost_probe.read_limits", AsyncMock(return_value=usage)), \
             patch("scripts.preheat_cost_probe.compact", AsyncMock(side_effect=fake_compact)), \
             contextlib.redirect_stdout(io.StringIO()):
            await run("unused", "gpt-6-luna", int(time.time()) - 1)
        self.assertEqual(events, ["model", "compact"])
        self.assertEqual(p.state["preheat_safety_model"], "gpt-6.1-sol")

    async def test_probe_never_starts_if_window_is_active(self):
        p = SimpleNamespace(state={}, _save=Mock(), run=AsyncMock())
        c = SimpleNamespace(preheater=p)
        usage = {"ok": True, "five_hour": {"used_percent": 1}}
        with patch.dict(os.environ, {}, clear=False), \
             patch("scripts.preheat_cost_probe.Collector", return_value=c), \
             patch("scripts.preheat_cost_probe.backup_and_claim") as claim, \
             patch("scripts.preheat_cost_probe.read_limits", AsyncMock(return_value=usage)):
            with self.assertRaises(RuntimeError):
                await run("unused", "gpt-6-luna", int(time.time()) - 1)
        claim.assert_not_called()
        p.run.assert_not_called()

    async def test_compaction_waits_for_confirmation(self):
        proc = FakeProcess([
            {"id": 3, "result": {}},
            {"method": "item/completed", "params": {"threadId": "t", "item": {"type": "contextCompaction"}}},
            {"method": "turn/completed", "params": {"threadId": "t", "turn": {"status": "completed"}}},
        ])
        p = Preheater("fixture")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), patch.object(p, "_rpc", AsyncMock()):
            self.assertIsNone(await compact(p, "t"))
        self.assertIn(b"thread/compact/start", b"".join(proc.stdin.writes))

    async def test_rejected_compaction_does_not_continue(self):
        proc = FakeProcess([{"id": 3, "error": {"code": -1}}])
        p = Preheater("fixture")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), patch.object(p, "_rpc", AsyncMock()):
            with self.assertRaises(RuntimeError):
                await compact(p, "t")

    async def test_turn_complete_without_compaction_fails_closed(self):
        proc = FakeProcess([{"id": 3, "result": {}}, {"method": "turn/completed",
                           "params": {"threadId": "t", "turn": {"status": "completed"}}}])
        p = Preheater("fixture")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), patch.object(p, "_rpc", AsyncMock()):
            with self.assertRaises(RuntimeError):
                await compact(p, "t")
