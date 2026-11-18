#!/usr/bin/env python3
"""Create a private build config from a local relay credential file; never print secrets."""
import argparse
import json
import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit

from device_config import build_header


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=Path, default=Path("device.local.json"))
    parser.add_argument("--relay-env", type=Path, required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist-private/relay-device.local.json"))
    parser.add_argument("--home-ssid", action="append", required=True)
    args = parser.parse_args()
    values = {}
    for line in args.relay_env.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        parsed = shlex.split(value, posix=True)
        if len(parsed) != 1:
            raise ValueError("invalid credential file entry")
        values[key.strip()] = parsed[0]
    url = urlsplit(values["HOME_PUBLIC_URL"])
    if url.scheme != "https" or not url.hostname or url.path not in ("", "/") or url.username or url.password or url.query or url.fragment:
        raise ValueError("relay URL must be an HTTPS origin")
    config = json.loads(args.device.read_text())
    config["home_relay"] = {"host": url.hostname, "port": url.port or 443,
                            "ca_pem": args.ca.read_text(), "read_token": values["HOME_READ_TOKEN"],
                            "device_id": values["HOME_DEVICE_ID"], "device_secret": values["HOME_DEVICE_SECRET"],
                            "home_ssids": args.home_ssid}
    build_header(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(args.output, flags, 0o600), "w") as out:
        json.dump(config, out, ensure_ascii=False)
        out.write("\n")
    print("Private relay build configuration prepared (mode 0600).")


if __name__ == "__main__":
    main()
