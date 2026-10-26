"""Validate private build configuration without logging secrets."""
import hashlib
import json


def build_header(config):
    required = {"wifi_networks", "server_host", "server_port"}
    if set(config) != required:
        raise ValueError("device config requires exactly wifi_networks, server_host, server_port")
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
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=True).encode()
    def literal(value):
        return '"' + ''.join('\\%03o' % b for b in value.encode()) + '"'
    return '\n'.join([
        '#pragma once',
        'struct XmNetwork { const char *ssid; const char *password; };',
        'static const XmNetwork XM_NETWORKS[] = {',
        *['{' + literal(n["ssid"]) + ',' + literal(n["password"]) + '},' for n in networks],
        '};',
        'static const unsigned XM_NETWORK_COUNT = sizeof(XM_NETWORKS) / sizeof(XM_NETWORKS[0]);',
        'static const char XM_BOOT_HOST[] = ' + literal(host) + ';',
        'static const unsigned short XM_BOOT_PORT = %d;' % port,
        'static const char XM_BOOT_REV[] = "%s";' % hashlib.sha256(canonical).hexdigest(),
        '',
    ])
