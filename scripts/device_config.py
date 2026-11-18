"""Validate private build configuration without logging secrets."""
import hashlib
import json
import re


def build_header(config):
    required = {"wifi_networks", "server_host", "server_port"}
    optional = {"preheat_token", "home_relay"}
    if not required.issubset(config) or set(config) - required - optional:
        raise ValueError("device config requires wifi_networks, server_host, server_port; optional preheat_token and home_relay")
    for key in ("server_host",):
        if not isinstance(config[key], str) or "\0" in config[key]:
            raise ValueError("device configuration strings must not contain NUL")
    networks = config["wifi_networks"]
    if not isinstance(networks, list) or not 1 <= len(networks) <= 5:
        raise ValueError("Provide 1–5 Wi-Fi networks")
    for network in networks:
        if not isinstance(network, dict) or set(network) != {"ssid", "password"}:
            raise ValueError("Each network requires ssid and password")
        ssid, password = network["ssid"], network["password"]
        if not isinstance(ssid, str) or "\0" in ssid or not 1 <= len(ssid.encode()) <= 32:
            raise ValueError("Wi-Fi SSID must be 1–32 UTF-8 bytes")
        if not isinstance(password, str) or "\0" in password or (password and not 8 <= len(password.encode()) <= 63):
            raise ValueError("Wi-Fi password must be empty or 8–63 UTF-8 bytes")
    host = config["server_host"]
    if not host or len(host) > 253 or any(c.isspace() or c in '/:?#' for c in host):
        raise ValueError("server_host must be a bare IPv4 address or hostname")
    port = config["server_port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("server_port must be an integer from 1 to 65535")
    preheat_token = config.get("preheat_token", "")
    if not isinstance(preheat_token, str) or "\0" in preheat_token or len(preheat_token) > 128:
        raise ValueError("preheat_token must be an optional string up to 128 characters")
    relay = config.get("home_relay")
    if relay is not None:
        keys = {"host", "port", "ca_pem", "read_token", "device_id", "device_secret", "home_ssids"}
        if not isinstance(relay, dict) or set(relay) != keys:
            raise ValueError("home_relay must contain host, port, ca_pem, read_token, device_id, device_secret and home_ssids")
        if not isinstance(relay["host"], str) or not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", relay["host"]):
            raise ValueError("home relay host must be a bare hostname")
        if type(relay["port"]) is not int or not 1 <= relay["port"] <= 65535:
            raise ValueError("home relay port must be a valid integer")
        if not isinstance(relay["ca_pem"], str) or "\0" in relay["ca_pem"] or "-----BEGIN CERTIFICATE-----" not in relay["ca_pem"] or "-----END CERTIFICATE-----" not in relay["ca_pem"]:
            raise ValueError("home relay requires a PEM CA certificate")
        for key, minimum in (("read_token", 32), ("device_secret", 32), ("device_id", 1)):
            value = relay[key]
            if not isinstance(value, str) or not minimum <= len(value) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("invalid home relay credential: " + key)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", relay["device_id"]):
            raise ValueError("invalid home relay device id")
        if relay["read_token"] == relay["device_secret"]:
            raise ValueError("home relay read and command credentials must be distinct")
        ssids = relay["home_ssids"]
        if not isinstance(ssids, list) or not 1 <= len(ssids) <= 5 or any(not isinstance(s, str) or not 1 <= len(s.encode()) <= 32 or "\0" in s for s in ssids):
            raise ValueError("home_ssids must contain 1–5 valid Wi-Fi names")
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=True).encode()
    def literal(value):
        return '"' + ''.join('\\%03o' % b for b in value.encode()) + '"'
    lines = [
        '#pragma once',
        'struct XmNetwork { const char *ssid; const char *password; };',
        'static const XmNetwork XM_NETWORKS[] = {',
        *['{' + literal(n["ssid"]) + ',' + literal(n["password"]) + '},' for n in networks],
        '};',
        'static const unsigned XM_NETWORK_COUNT = sizeof(XM_NETWORKS) / sizeof(XM_NETWORKS[0]);',
        'static const char XM_BOOT_HOST[] = ' + literal(host) + ';',
        'static const unsigned short XM_BOOT_PORT = %d;' % port,
        'static const char XM_PREHEAT_TOKEN[] = ' + literal(preheat_token) + ';',
        'static const char XM_BOOT_REV[] = "%s";' % hashlib.sha256(canonical).hexdigest(),
    ]
    if relay is not None:
        lines += [
            '#define XM_HOME_RELAY_CONFIGURED 1',
            'static const char XM_RELAY_HOST[] = ' + literal(relay["host"]) + ';',
            'static const unsigned short XM_RELAY_PORT = %d;' % relay["port"],
            'static const char XM_RELAY_CA_PEM[] = ' + literal(relay["ca_pem"]) + ';',
            'static const char XM_RELAY_READ_TOKEN[] = ' + literal(relay["read_token"]) + ';',
            'static const char XM_RELAY_DEVICE_ID[] = ' + literal(relay["device_id"]) + ';',
            'static const char XM_RELAY_DEVICE_SECRET[] = ' + literal(relay["device_secret"]) + ';',
            'static const char *const XM_HOME_SSIDS[] = {' + ','.join(literal(s) for s in relay["home_ssids"]) + '};',
            'static const unsigned XM_HOME_SSID_COUNT = sizeof(XM_HOME_SSIDS) / sizeof(XM_HOME_SSIDS[0]);',
        ]
    return '\n'.join(lines + [''])
