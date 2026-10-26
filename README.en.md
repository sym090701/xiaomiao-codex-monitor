# XiaoMiao Codex Usage Display

Show Codex five-hour and weekly remaining usage, reset timers, and a Mac performance page on the Xueersi XiaoMiao handheld. Codex login credentials stay on the Mac; the ESP32 firmware does not store account tokens.

[简体中文](README.md) · [Upstream and license notes](UPSTREAM.md)

## Status

- Target: original ESP32-based XiaoMiao, ESP32-D0WD, 4 MB flash, ST7735 160 × 128 display.
- Build: firmware compiles; companion parsing and state tests pass; the Launcher app image is about 1 MiB.
- Hardware validation: this port has not yet been boot-tested on a physical XiaoMiao. Display colors, buttons, and Wi-Fi still need device testing.
- The Codex usage endpoint is undocumented and may change.

## Controls and features

- Shows remaining Codex five-hour and weekly quota and reset countdowns.
- Press A to switch between Codex usage and Mac CPU, memory, disk, load, and network pages.
- Press B to reconnect to the computer service. Hold A+B for about 1.5 seconds to return to Launcher.
- A setup Wi-Fi hotspot gets a unique password on first boot; the password is shown on screen.
- A local Mac service reads Codex usage and sends display data over Wi-Fi/WebSocket.

The original XiaoMiao backlight has no PWM pin controlled by this port, so the web brightness setting does not change handheld brightness.

## Hardware map

| Signal | ESP32 GPIO | Function |
|---|---:|---|
| TFT CS | 5 | ST7735 SPI chip select |
| TFT DC | 4 | Data/command |
| TFT RST | 19 | Display reset |
| TFT SCLK | 18 | SPI clock |
| TFT MOSI | 23 | SPI data |
| SD CS | 22 | Held high to release the shared SPI bus |
| A button | 34 | Active low |
| B button | 12 | Active low |

This build targets the ESP32 XiaoMiao, not an ESP32-C3 SuperMini or another 160 × 80 display. Verify pins on other hardware revisions.

## Install with Launcher

1. Build the firmware and copy `.pio/build/xiaomiao/firmware.bin` to `/boot/` on a TF card.
2. Select the BIN in Launcher and install it. Allocate at least `0x100000` (1 MiB, 64 KiB aligned).
3. The installed app no longer needs the TF card.

Do not write this application image at `0x0` or `0x10000`. The port is build-verified, not device-boot-verified. Confirm Launcher free space before installing.

## Build

Two build variants are available: `xiaomiao` (web provisioning) and
`xiaomiao_preconfigured` (private `device.local.json`, multiple Wi-Fi networks).
Use `python3 scripts/build_xiaomiao.py --configure` for an interactive setup and build.
Private BINs contain Wi-Fi credentials and must never be published.
See [configuration and Mac relay instructions](docs/CONFIGURATION.md).
The Ubuntu collector now accepts authenticated Mac performance uploads; after 45 seconds
without a heartbeat it reports the Mac offline while continuing Codex quota collection.

Install PlatformIO Core, then run:

```bash
python3 -m pip install platformio
pio run
```

The default PlatformIO environment is `xiaomiao`. Build-time preprocessing embeds the setup page, so a separate LittleFS upload is not needed.

## Mac service

Requires macOS, Python 3.10+, and a signed-in local Codex environment.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
HOST=192.168.1.20 PORT=8080 PYTHON_BIN="$PWD/.venv/bin/python" ./server.sh start
```

Replace `192.168.1.20` with the Mac's current LAN address. Keep the Mac and XiaoMiao on the same trusted network, and allow the port through the local firewall. Open `http://<Mac-LAN-IP>:8080` on the Mac for the dashboard, matching the HOST binding. Stop the service with `./server.sh stop`.

The service API has no device authentication. Do not expose it to the public Internet or configure router port forwarding. Never commit `auth.json`, runtime logs, local settings, or Wi-Fi credentials.

## Tests and source

Run `python3 -m unittest discover -s test -v` for offline companion tests. CI runs the tests and builds the firmware; this does not replace physical device validation.

This XiaoMiao port is based on [evoking6/esp32-code-monitor](https://github.com/evoking6/esp32-code-monitor). Its source history is retained to represent upstream contributions. See [UPSTREAM.md](UPSTREAM.md) for the baseline and license status.
