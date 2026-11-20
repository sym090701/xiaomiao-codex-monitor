import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from quota_server import Preheater
from scripts.preheat_cost_probe import compact


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
