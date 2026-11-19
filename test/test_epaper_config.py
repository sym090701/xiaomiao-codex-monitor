import unittest

from scripts.epaper_config import build_epaper_header


class EpaperConfigTest(unittest.TestCase):
    def test_unneeded_relay_secrets_do_not_change_generated_header(self):
        config = {"wifi_networks": [{"ssid": "fixture", "password": "fixturepass"}],
                  "server_host": "192.0.2.1", "server_port": 8765}
        before = build_epaper_header(config)
        config["preheat_token"] = "unnecessary-token"
        config["home_relay"] = {"device_secret": "private-unused-key"}
        self.assertEqual(before, build_epaper_header(config))
        self.assertNotIn("XM_RELAY", before)

    def test_requires_network_settings(self):
        with self.assertRaises(KeyError):
            build_epaper_header({})
