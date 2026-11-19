"""Restrict experimental e-paper builds to local Wi-Fi and quota settings."""
from scripts.device_config import build_header


def build_epaper_header(config):
    local_only = {key: config[key] for key in ("wifi_networks", "server_host", "server_port")}
    return build_header(local_only)
