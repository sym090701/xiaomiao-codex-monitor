import unittest
from scripts.device_config import build_header


class BuildConfigTest(unittest.TestCase):
    def config(self):
        return {"wifi_networks": [{"ssid": '测试"\\network', "password": "test-only-password"},
                                  {"ssid": "backup", "password": ""}],
                "server_host": "192.168.1.153", "server_port": 8765}

    def test_safe_unicode_escape_and_revision(self):
        header = build_header(self.config())
        self.assertNotIn("测试", header)
        self.assertIn("XM_NETWORK_COUNT", header)
        self.assertEqual(header, build_header(self.config()))
        other = self.config()
        other["server_port"] = 8080
        self.assertNotEqual(header, build_header(other))

    def test_invalid_config(self):
        for key, value in (("server_port", True), ("server_host", "http://host"),
                           ("wifi_networks", [])):
            with self.subTest(key=key):
                config = self.config()
                config[key] = value
                with self.assertRaises(ValueError):
                    build_header(config)

    def test_reject_missing_password(self):
        config = self.config()
        config["wifi_networks"][0].pop("password")
        with self.assertRaises(ValueError):
            build_header(config)

    def test_optional_private_relay_configuration(self):
        config = self.config()
        config["home_relay"] = {"host": "relay.example.com", "port": 443,
                                "ca_pem": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
                                "read_token": "r" * 32, "device_id": "xm_1",
                                "device_secret": "s" * 32, "home_ssids": ["home"]}
        self.assertIn("XM_HOME_RELAY_CONFIGURED", build_header(config))
        config["home_relay"]["host"] = "http://relay.example.com"
        with self.assertRaises(ValueError):
            build_header(config)
        config["home_relay"]["host"] = "relay.example.com"
        config["home_relay"]["device_secret"] = "r" * 32
        with self.assertRaises(ValueError):
            build_header(config)
