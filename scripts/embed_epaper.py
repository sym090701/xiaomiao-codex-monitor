"""Embed only the Wi-Fi and local quota server needed by the ESP8266 build."""
import json
import os
from pathlib import Path
import sys

Import("env")
root = Path(env.subst("$PROJECT_DIR"))
sys.path.insert(0, str(root))
from scripts.epaper_config import build_epaper_header

config_path = Path(os.environ.get("XM_DEVICE_CONFIG_FILE", str(root / "device.example.json")))
config = json.loads(config_path.read_text(encoding="utf-8"))
header = build_epaper_header(config)
build_dir = Path(env.subst("$BUILD_DIR"))
build_dir.mkdir(parents=True, exist_ok=True)
(build_dir / "xiaomiao_boot_config.h").write_text(header, encoding="utf-8")
env.Append(CPPPATH=[str(build_dir)])
