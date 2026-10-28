#!/usr/bin/env python3
"""Discover and update XiaoMiao over authenticated LAN HTTP OTA."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import time

import requests
from requests.auth import HTTPDigestAuth

MARKER = b"XIAOMIAO_CODEX_STANDALONE_OTA_V1"


def image_digest(data):
    if len(data) < 256 or data[0] != 0xe9 or not 0 < data[1] <= 16 or data[12:14] != b"\0\0":
        raise ValueError("Expected an ESP32 app BIN, not a merged/full-device image")
    pos = 24
    checksum = 0xef
    for _ in range(data[1]):
        if pos + 8 > len(data):
            raise ValueError("Truncated segment header")
        _, length = struct.unpack_from("<II", data, pos)
        pos += 8
        if pos + length > len(data):
            raise ValueError("Truncated segment")
        for b in data[pos:pos + length]:
            checksum ^= b
        pos += length
    end = (pos + 16) & ~15
    if end > len(data) or data[end - 1] != checksum or data[23] != 1:
        raise ValueError("Invalid app checksum or missing image hash")
    digest = hashlib.sha256(data[:end]).digest()
    if data[end:end + 32] != digest or len(data) != end + 32:
        raise ValueError("Invalid image hash or trailing data")
    if MARKER not in data:
        raise ValueError("This is not a XiaoMiao standalone OTA image")
    return digest.hex()


def discover(seconds=4):
    try:
        from zeroconf import Zeroconf, ServiceBrowser
    except ImportError:
        raise SystemExit("Install discovery support: python3 -m pip install zeroconf")
    devices = {}
    class Listener:
        def add_service(self, zc, service_type, name):
            info = zc.get_service_info(service_type, name)
            if info and info.properties.get(b"target") == b"xiaomiao-esp32":
                devices[name] = {"name": name, "host": info.server.rstrip("."),
                                 "port": info.port, "addresses": info.parsed_addresses()}
        def update_service(self, *args):
            self.add_service(*args)
        def remove_service(self, zc, service_type, name):
            devices.pop(name, None)
    with Zeroconf() as zc:
        browser = ServiceBrowser(zc, "_xiaomiao-ota._tcp.local.", Listener())
        time.sleep(seconds)
        browser.cancel()
    return list(devices.values())


def upload(host, port, password_file, image):
    if not host or any(c in host for c in "/:@?#") or any(c.isspace() for c in host):
        raise ValueError("Use a bare hostname or IPv4 address")
    config = json.loads(password_file.read_text())
    data = image.read_bytes()
    expected = image_digest(data)
    base = "http://%s:%d" % (host, port)
    with requests.Session() as session:
        session.trust_env = False
        session.auth = HTTPDigestAuth(config["username"], config["password"])
        def get(path):
            response = session.get(base + path, timeout=10, allow_redirects=False)
            response.raise_for_status()
            if response.is_redirect:
                raise RuntimeError("Redirect rejected")
            return response
        info = get("/api/device-info").json()
        if info.get("standalone_ota") is not True:
            raise ValueError("Device does not have standalone OTA enabled")
        before = get("/api/ota").json()
        if len(data) > before["slot_size"]:
            raise ValueError("Firmware exceeds device OTA slot")
        nonce = re.search(r"action='/ota\?nonce=([0-9a-f]+)'", get("/ota").text)
        if not nonce:
            raise RuntimeError("OTA nonce missing")
        response = session.post(base + "/ota?nonce=" + nonce.group(1),
                                files={"firmware": ("firmware.bin", data, "application/octet-stream")},
                                timeout=120, allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("Device rejected OTA (HTTP %d)" % response.status_code)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                after = get("/api/ota").json()
                if after["running"] != before["running"] and after.get("image_sha256") == expected:
                    print("OTA verified: %s -> %s, SHA-256 %s" % (before["running"], after["running"], expected))
                    return
            except (requests.RequestException, ValueError):
                pass
        raise RuntimeError("Upload accepted, but reboot/slot/hash verification timed out; inspect device")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--host", default="codexusage.local")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--password-file", type=Path, default=Path(__file__).resolve().parents[1] / "ota.local.json")
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    if args.discover:
        print(json.dumps(discover(), ensure_ascii=False, indent=2))
    elif not args.image:
        parser.error("--image is required for OTA")
    else:
        upload(args.host, args.port, args.password_file, args.image)
