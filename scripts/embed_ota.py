import json
import secrets
from pathlib import Path
Import("env")
root = Path(env.subst("$PROJECT_DIR"))
path = root / "ota.local.json"
if not path.exists():
    path.touch(mode=0o600)
    path.write_text(json.dumps({"username": "admin", "password": secrets.token_hex(8)}) + "\n")
path.chmod(0o600)
config = json.loads(path.read_text())
password = config.get("password", "")
if config.get("username") != "admin" or not isinstance(password, str) or not 12 <= len(password) <= 32 or not password.isalnum():
    raise ValueError("ota.local.json requires username admin and a 12–32 character alphanumeric password")
build = Path(env.subst("$BUILD_DIR"))
build.mkdir(parents=True, exist_ok=True)
(build / "xiaomiao_ota_secret.h").write_text('#pragma once\nstatic const char XM_OTA_PASSWORD[] = ' + json.dumps(password) + ';\n')
env.Append(CPPPATH=[str(build)])
