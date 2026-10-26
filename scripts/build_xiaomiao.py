#!/usr/bin/env python3
"""One entry point for both public portal and private preconfigured firmware."""
import argparse
import getpass
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Build XiaoMiao firmware")
    parser.add_argument("--configured", action="store_true", help="Embed device.local.json (private BIN)")
    parser.add_argument("--configure", action="store_true", help="Create private config interactively, then build")
    args = parser.parse_args()
    configured = args.configured or args.configure
    if args.configure:
        from device_config import build_header
        networks = []
        while True:
            ssid = input("Wi-Fi name: ").strip()
            password = getpass.getpass("Wi-Fi password (hidden): ")
            networks.append({"ssid": ssid, "password": password})
            if len(networks) >= 5 or input("Add backup Wi-Fi? [y/N]: ").lower() != "y":
                break
        config = {"wifi_networks": networks,
                  "server_host": input("Server address [192.168.1.153]: ").strip() or "192.168.1.153",
                  "server_port": int(input("Server port [8765]: ").strip() or "8765")}
        build_header(config)
        path = ROOT / "device.local.json"
        if path.exists() and input("Replace existing private config? [y/N]: ").lower() != "y":
            raise SystemExit("Existing config kept")
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    environment = "xiaomiao_preconfigured" if configured else "xiaomiao"
    if configured and not (ROOT / "device.local.json").exists():
        raise SystemExit("Run with --configure or fill device.local.json first")
    executable = shutil.which("pio") or shutil.which("platformio")
    fallback = Path.home() / ".platformio/penv/bin/platformio"
    if not executable and fallback.exists():
        executable = str(fallback)
    if not executable:
        raise SystemExit("Install PlatformIO first: python3 -m pip install platformio")
    subprocess.run([executable, "run", "-e", environment], cwd=ROOT, check=True)
    output = ROOT / ("dist-private" if configured else "dist")
    output.mkdir(exist_ok=True)
    name = "XiaoMiao-Preconfigured-PRIVATE.bin" if configured else "XiaoMiao-Portal-Launcher.bin"
    target = output / name
    shutil.copyfile(ROOT / ".pio/build" / environment / "firmware.bin", target)
    if configured:
        target.chmod(0o600)
    print("Firmware: " + str(target))
    if configured:
        print("PRIVATE: contains Wi-Fi credentials. Do not upload or share this BIN.")


if __name__ == "__main__":
    main()
