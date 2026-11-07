import tempfile
import unittest
from pathlib import Path

from main import PushService


class SecurityHardeningTest(unittest.TestCase):
    def test_hook_transcript_must_stay_under_codex_home(self):
        service = PushService("https://example.invalid/usage")
        with tempfile.TemporaryDirectory() as root:
            service.codex_home = Path(root).resolve()
            with self.assertRaises(ValueError):
                service.update_codex_status("UserPromptSubmit", "attacker", "/tmp/secret")

    def test_hook_session_limit_is_enforced(self):
        service = PushService("https://example.invalid/usage")
        service.codex_session_limit = 1
        with tempfile.TemporaryDirectory() as root:
            service.codex_home = Path(root).resolve()
            transcript = Path(root) / "one.jsonl"
            transcript.write_text("", encoding="utf-8")
            service.update_codex_status("UserPromptSubmit", "one", str(transcript))
            with self.assertRaises(OverflowError):
                service.update_codex_status("UserPromptSubmit", "two", str(transcript))
