import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class EpaperQuotaLogicTest(unittest.TestCase):
    def test_reset_time_change_detection_and_throttle(self):
        compiler = shutil.which("c++")
        if not compiler:
            self.skipTest("C++ compiler unavailable")
        source = Path(__file__).with_name("epaper_quota_logic_test.cpp")
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "epaper-logic-test"
            subprocess.run([compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror",
                            str(source), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)
