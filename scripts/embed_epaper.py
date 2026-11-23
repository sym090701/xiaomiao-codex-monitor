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

# Keep the ESP8266 HTTP OTA credential out of source control and public
# artifacts.  Reuse the local private OTA file used by the XiaoMiao build;
# callers can override it for a separate e-paper credential.
ota_path = Path(os.environ.get("EPAPER_OTA_CONFIG_FILE", str(root / "ota.local.json")))
if not ota_path.exists():
    raise ValueError("ESP8266 e-paper OTA requires a local OTA config file")
ota = json.loads(ota_path.read_text(encoding="utf-8"))
ota_user = str(ota.get("username", "admin"))
ota_password = str(ota.get("password", ""))
if not ota_user or not ota_password or len(ota_password) < 12:
    raise ValueError("ESP8266 e-paper OTA password must be at least 12 characters")
header += "static const char XM_EPD_OTA_USER[] = " + json.dumps(ota_user) + ";\n"
header += "static const char XM_EPD_OTA_PASSWORD[] = " + json.dumps(ota_password) + ";\n"
build_dir = Path(env.subst("$BUILD_DIR"))
build_dir.mkdir(parents=True, exist_ok=True)
(build_dir / "xiaomiao_boot_config.h").write_text(header, encoding="utf-8")
env.Append(CPPPATH=[str(build_dir)])
