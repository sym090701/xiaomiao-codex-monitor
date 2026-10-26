"""Push Mac performance to the Ubuntu collector; no Codex credentials accessed."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from urllib.request import Request
from urllib.parse import urlsplit

from main import performance_collector

LABEL = "io.github.xiaomiao.mac-agent"


def read_config(path):
    config = json.loads(path.read_text())
    url = config["server_url"].rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError("server_url must be an http(s) origin")
    token = Path(config["token_file"]).expanduser().read_text().strip()
    if len(token) < 32:
        raise ValueError("Missing or invalid agent token")
    return url, token


def push(url, token):
    metrics = performance_collector.collect()
    request = Request(url + "/api/mac/metrics", data=json.dumps(metrics).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    # Reject redirects so bearer credentials cannot move to another destination.
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        if response.status != 200 or json.load(response).get("ok") is not True:
            raise RuntimeError("Metrics upload failed")


def install(config):
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent installation requires macOS")
    read_config(config)
    path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    path.parent.mkdir(parents=True, exist_ok=True)
    logdir = Path.home() / "Library/Logs/XiaoMiao"
    logdir.mkdir(parents=True, exist_ok=True)
    service = {"Label": LABEL, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
               "--config", str(config)], "RunAtLoad": True, "KeepAlive": True,
               "ThrottleInterval": 10, "StandardOutPath": str(logdir / "mac-agent.log"),
               "StandardErrorPath": str(logdir / "mac-agent.error.log")}
    domain = "gui/" + str(os.getuid())
    if path.exists():
        subprocess.run(["launchctl", "bootout", domain, str(path)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with path.open("wb") as handle:
        plistlib.dump(service, handle)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    print("Mac agent installed for login startup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("mac-agent.local.json"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--setup", metavar="SERVER_ORIGIN", help="Prepare private config and token")
    args = parser.parse_args()
    config = args.config.resolve()
    if args.setup:
        import secrets
        token_path = config.with_name("mac-agent.token")
        if not token_path.exists():
            token_path.touch(mode=0o600)
            token_path.write_text(secrets.token_hex(32) + "\n")
        token_path.chmod(0o600)
        if not config.exists():
            config.touch(mode=0o600)
            config.write_text(json.dumps({"server_url": args.setup,
                                         "token_file": str(token_path)}, indent=2) + "\n")
        config.chmod(0o600)
        read_config(config)
        print("Private Mac agent config prepared")
    elif args.install:
        install(config)
    else:
        url, token = read_config(config)
        while True:
            try:
                push(url, token)
                if args.once:
                    print("Mac metrics uploaded")
                    break
            except Exception as exc:
                print("Mac metrics upload failed: " + type(exc).__name__, flush=True)
                if args.once:
                    sys.exit(1)
            time.sleep(10)
