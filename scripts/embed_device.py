import json
import sys
from pathlib import Path
Import("env")
root = Path(env.subst("$PROJECT_DIR"))
sys.path.insert(0, str(root / "scripts"))
from device_config import build_header
config_file = root / "device.local.json"
if not config_file.exists():
    raise RuntimeError("Copy device.example.json to device.local.json and fill Wi-Fi settings first")
header = build_header(json.loads(config_file.read_text(encoding="utf-8")))
build_dir = Path(env.subst("$BUILD_DIR"))
build_dir.mkdir(parents=True, exist_ok=True)
(build_dir / "xiaomiao_boot_config.h").write_text(header, encoding="utf-8")
env.Append(CPPPATH=[str(build_dir)])
