import json
import tempfile
import unittest
from pathlib import Path

import main
from hooks import codex_display_hook


class CodexStatusTest(unittest.TestCase):
    def test_post_tool_use_resumes_working_state_after_permission(self):
        service = main.PushService("unused")

        waiting = service.update_codex_status(
            "PermissionRequest", "session-1", None
        )
        working = service.update_codex_status("PostToolUse", "session-1", None)

        self.assertEqual("permission", waiting["state"])
        self.assertEqual("working", working["state"])
        self.assertEqual("PostToolUse", working["event"])
        self.assertEqual(1, working["active_conversations"])


class HookInstallTest(unittest.TestCase):
    def test_install_includes_post_tool_use(self):
        original_hooks_file = codex_display_hook.HOOKS_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                hooks_file = Path(directory) / "hooks.json"
                hooks_file.write_text(
                    json.dumps({
                        "hooks": {
                            "CustomEvent": [{
                                "hooks": [{
                                    "type": "command",
                                    "command": "keep-me",
                                }],
                            }],
                        },
                    }),
                    encoding="utf-8",
                )
                codex_display_hook.HOOKS_FILE = hooks_file

                codex_display_hook.install()

                document = json.loads(hooks_file.read_text(encoding="utf-8"))
                self.assertIn("PostToolUse", document["hooks"])
                self.assertIn("CustomEvent", document["hooks"])
                self.assertTrue(codex_display_hook.installed())
        finally:
            codex_display_hook.HOOKS_FILE = original_hooks_file


if __name__ == "__main__":
    unittest.main()
