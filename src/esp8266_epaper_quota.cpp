// Experimental 296x152 tri-color panel profile. Not hardware-validated.
// The manufacturer example and supplied panel specification disagree about BUSY.
// Do not upload until the panel/controller and electrical compatibility are confirmed.
// Display sequence/plane conventions adapted from sym090701's MIT-licensed
// esp8266-e2266fs092-photo-frame; see docs/licenses/esp8266-photo-frame-MIT.txt.
#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266HTTPUpdateServer.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <ESP8266mDNS.h>
#include <SPI.h>
#include "xiaomiao_boot_config.h"
#include "esp8266_epaper_quota_logic.h"

namespace {
constexpr uint8_t PIN_BUSY = D1, PIN_RESET = D4, PIN_DC = D2, PIN_CS = D8;
// NodeMCU/Waveshare ESP8266 BOOT/FLASH key: GPIO0 (D3), active low.
constexpr uint8_t PIN_BOOT = D3;
constexpr uint16_t PANEL_WIDTH = 152, PANEL_HEIGHT = 296;
constexpr size_t PLANE_BYTES = PANEL_WIDTH * PANEL_HEIGHT / 8;
constexpr unsigned long POLL_MS = 60UL * 1000UL;
constexpr unsigned long MIN_REFRESH_MS = 3UL * 60UL * 1000UL;
constexpr unsigned long BUSY_TIMEOUT_MS = 60000UL;
// Match the requested FLASH-button semantics: debounce first, then trigger
// the white refresh at the long-press threshold while the LED stays on.
constexpr unsigned long BOOT_DEBOUNCE_MS = 40UL;
constexpr unsigned long BOOT_INDICATE_MS = 1500UL;
#ifndef EPD_BUSY_LEVEL
// Matches the supplied ESP8266 demo, NOT the conflicting panel PDF.
#define EPD_BUSY_LEVEL LOW
#endif
#ifndef EPD_ALLOW_UNVERIFIED_DRIVER
#define EPD_ALLOW_UNVERIFIED_DRIVER 0
#endif
GFXcanvas1 canvas(PANEL_WIDTH, PANEL_HEIGHT);
ESP8266WebServer otaServer(80);
ESP8266HTTPUpdateServer httpUpdater;

using Quota = EpaperQuota;

void sendByte(bool data, uint8_t value) {
  digitalWrite(PIN_CS, LOW);
  digitalWrite(PIN_DC, data ? HIGH : LOW);
  SPI.transfer(value);
  digitalWrite(PIN_CS, HIGH);
}
void command(uint8_t value) { sendByte(false, value); }
void data(uint8_t value) { sendByte(true, value); }

bool waitReady() {
  unsigned long start = millis();
  while (digitalRead(PIN_BUSY) == EPD_BUSY_LEVEL) {
    if (millis() - start >= BUSY_TIMEOUT_MS) {
      Serial.println(F("[EPD] BUSY timeout; check model, wiring and BUSY polarity"));
      return false;
    }
    delay(10);
  }
  return true;
}

bool panelStart() {
  digitalWrite(PIN_RESET, LOW); delay(20);
  digitalWrite(PIN_RESET, HIGH); delay(20);
  command(0x04);
  if (!waitReady()) return false;
  command(0x00); data(0x0f); data(0x89);
  command(0x01); data(0x03); data(0x00); data(0x21); data(0x21); data(0x01);
  command(0x41); data(0x07);
  command(0x61); data(0x98); data(0x01); data(0x28);
  command(0x50); data(0x97);
  command(0x2a); data(0x00); data(0x00); data(0x00); data(0xff); data(0x00);
  return true;
}

bool panelDisplay() {
#if !EPD_ALLOW_UNVERIFIED_DRIVER
  Serial.println(F("[EPD] disabled: confirm controller and BUSY polarity before enabling"));
  return false;
#else
  Serial.println(F("[EPD] refresh started"));
  SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
  if (!panelStart()) { SPI.endTransaction(); return false; }
  command(0x10);
  for (size_t i = 0; i < PLANE_BYTES; ++i) {
    data(canvas.getBuffer()[i]);
    if ((i & 255) == 0) yield();
  }
  command(0x13);
  for (size_t i = 0; i < PLANE_BYTES; ++i) {
    data(0x00); // DTM2: zero means no red, per the tested photo-frame driver.
    if ((i & 255) == 0) yield();
  }
  command(0x12); delay(20);
  if (!waitReady()) { SPI.endTransaction(); return false; }
  command(0x02);
  if (!waitReady()) { SPI.endTransaction(); return false; }
  delay(1000); command(0x07); data(0xa5);
  SPI.endTransaction();
  Serial.println(F("[EPD] refresh complete, panel asleep"));
  return true;
#endif
}

bool clearPanel() {
  if (!canvas.getBuffer()) return false;
  Serial.println(F("[CLEAN] BOOT long-press: white full refresh"));
  canvas.setRotation(1);
  canvas.fillScreen(1);
  if (!panelDisplay()) return false;
  Serial.println(F("[CLEAN] complete"));
  return true;
}

void pollBootButton(bool busy, bool &clearQueued) {
  static bool rawState = HIGH;
  static bool stableState = HIGH;
  static bool wasPressed = false;
  static bool indicateLong = false;
  static bool clearTriggered = false;
  static unsigned long changedAt = 0;
  static unsigned long pressedAt = 0;
  const unsigned long now = millis();
  const bool raw = digitalRead(PIN_BOOT);
  if (raw != rawState) {
    rawState = raw;
    changedAt = now;
  }
  if (now - changedAt >= BOOT_DEBOUNCE_MS && raw != stableState) {
    stableState = raw;
    if (stableState == LOW) {
      wasPressed = true;
      indicateLong = false;
      clearTriggered = false;
      pressedAt = now;
    } else if (wasPressed) {
      const unsigned long heldMs = now - pressedAt;
      wasPressed = false;
      indicateLong = false;
      digitalWrite(LED_BUILTIN, HIGH);
      if (busy) {
        Serial.println(F("[CLEAN] BOOT release ignored while display is busy"));
      } else if (heldMs >= BOOT_INDICATE_MS && !clearTriggered) {
        clearTriggered = true;
        clearQueued = true;
        Serial.println(F("[CLEAN] BOOT released; white clear queued"));
      } else {
        Serial.println(F("[CLEAN] BOOT press released; no clear"));
      }
    }
  }
  const unsigned long heldMs = now - pressedAt;
  if (wasPressed && stableState == LOW && !indicateLong &&
      heldMs >= BOOT_INDICATE_MS) {
    indicateLong = true;
    digitalWrite(LED_BUILTIN, LOW); // ESP8266 onboard LED is active-low.
    Serial.println(F("[CLEAN] BOOT 1.5s threshold reached; release to clear"));
  }
}

void text(int x, int y, uint8_t size, const String &value) {
  canvas.setCursor(x, y); canvas.setTextSize(size); canvas.setTextColor(0);
  canvas.print(value);
}
void metric(int y, const char *name, int value, uint32_t resetEpoch) {
  text(8, y, 2, name);
  String amount = value >= 0 ? String(value / 10) + "." + String(value % 10) + "%" : "--";
  text(196, y, 2, amount);
  char reset[12];
  text(8, y + 19, 1, epaperResetTime(resetEpoch, reset, sizeof(reset)) ? String("RESET ") + reset : "RESET --");
  canvas.drawRect(8, y + 29, 280, 10, 0);
  if (value >= 0) canvas.fillRect(10, y + 31, value * 276 / 1000, 6, 0);
}
bool drawQuota(const Quota &quota) {
  if (!canvas.getBuffer()) { Serial.println(F("[EPD] allocation failed")); return false; }
  canvas.setRotation(1); canvas.fillScreen(1); canvas.setTextWrap(false);
  text(8, 5, 2, "CODEX QUOTA");
  text(214, 8, 1, quota.valid ? "ONLINE" : "OFFLINE");
  metric(27, "5 HOURS", quota.fiveTenths, quota.fiveResetEpoch);
  metric(78, "WEEK", quota.weeklyTenths, quota.weeklyResetEpoch);
  text(8, 135, 1, "RESET: CHINA TIME");
  return panelDisplay();
}

int remaining(JsonVariantConst window) {
  if (!window.is<JsonObjectConst>() || !window["remaining_percent"].is<float>()) return -1;
  float value = window["remaining_percent"].as<float>();
  if (!isfinite(value) || value < 0 || value > 100) return -1;
  return int(value * 10.0f + 0.5f);
}

uint32_t resetEpoch(JsonVariantConst window) {
  if (!window.is<JsonObjectConst>() || !window["reset_at_epoch"].is<double>()) return 0;
  double value = window["reset_at_epoch"].as<double>();
  if (!isfinite(value) || value < 946684800.0 || value > 4294967295.0) return 0;
  return uint32_t(value);
}

Quota readQuota() {
  Quota out;
  if (WiFi.status() != WL_CONNECTED) return out;
  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, XM_BOOT_HOST, XM_BOOT_PORT, "/usage")) return out;
  http.setTimeout(5000);
  int status = http.GET();
  Serial.printf("[Quota] HTTP %d\n", status);
  int length = http.getSize();
  if (status == HTTP_CODE_OK && length >= 0 && length <= 8192) {
    DynamicJsonDocument doc(8192);
    if (!deserializeJson(doc, http.getStream()) && doc["ok"].as<bool>() && !doc["stale"].as<bool>()) {
      out.fiveTenths = remaining(doc["five_hour"]);
      out.weeklyTenths = remaining(doc["weekly"]);
      out.valid = out.fiveTenths >= 0 || out.weeklyTenths >= 0;
      if (out.fiveTenths >= 0) out.fiveResetEpoch = resetEpoch(doc["five_hour"]);
      if (out.weeklyTenths >= 0) out.weeklyResetEpoch = resetEpoch(doc["weekly"]);
    }
  }
  http.end();
  return out;
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  static unsigned network = 0;
  const XmNetwork &next = XM_NETWORKS[network++ % XM_NETWORK_COUNT];
  Serial.println(F("[WiFi] connecting"));
  WiFi.begin(next.ssid, next.password);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 12000) delay(100);
  if (WiFi.status() == WL_CONNECTED) Serial.println(F("[WiFi] connected"));
  else Serial.println(F("[WiFi] not connected"));
}

void startOta() {
  otaServer.on("/", HTTP_GET, []() {
    if (!otaServer.authenticate(XM_EPD_OTA_USER, XM_EPD_OTA_PASSWORD)) {
      return otaServer.requestAuthentication();
    }
    otaServer.send(200, "text/plain", "ESP8266 quota e-paper OTA: POST /update");
  });
  httpUpdater.setup(&otaServer, "/update", XM_EPD_OTA_USER, XM_EPD_OTA_PASSWORD);
  otaServer.begin();
  if (MDNS.begin("codex-epaper")) {
    MDNS.addService("http", "tcp", 80);
    Serial.println(F("[OTA] http://codex-epaper.local/update"));
  } else {
    Serial.println(F("[OTA] mDNS unavailable; use device IP /update"));
  }
}
}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUSY, INPUT);
  pinMode(PIN_BOOT, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);
  pinMode(PIN_RESET, OUTPUT); pinMode(PIN_DC, OUTPUT); pinMode(PIN_CS, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);
  digitalWrite(PIN_CS, HIGH); digitalWrite(PIN_RESET, HIGH);
  SPI.begin(); WiFi.mode(WIFI_STA);
}

void loop() {
#if defined(EPD_SERIAL_CLEAN_TEST) && EPD_SERIAL_CLEAN_TEST
  // A separate temporary build: never starts repeated refreshes by itself.
  // After reset it waits for an explicit serial command again.
  static bool cleaningComplete = false;
  static String serialCommand;
  if (!cleaningComplete) {
    while (Serial.available()) {
      char ch = char(Serial.read());
      if (ch == '\n') {
        serialCommand.trim();
        if (serialCommand == "CLEAN3") {
          if (!canvas.getBuffer()) {
            Serial.println(F("[CLEAN] FAILED: framebuffer allocation"));
            while (true) delay(1000);
          }
          Serial.println(F("[CLEAN] 3 black-white cycles requested"));
          for (unsigned cycle = 1; cycle <= 3; ++cycle) {
            for (unsigned white = 0; white < 2; ++white) {
              canvas.setRotation(1);
              canvas.fillScreen(white ? 1 : 0);
              Serial.printf("[CLEAN] cycle=%u phase=%s\n", cycle, white ? "white" : "black");
              if (!panelDisplay()) {
                Serial.println(F("[CLEAN] FAILED; stopped"));
                while (true) delay(1000);
              }
            }
          }
          cleaningComplete = true;
          Serial.println(F("[CLEAN] DONE; restoring quota display"));
        }
        serialCommand = "";
      } else if (serialCommand.length() < 16) {
        serialCommand += ch;
      }
    }
    if (!cleaningComplete) { delay(50); return; }
  }
#endif
  static bool first = true, hasPainted = false, hasAttempted = false;
  static unsigned long lastPoll = 0, lastAttempt = 0;
  static bool otaStarted = false;
  static bool clearQueued = false;
  static Quota previous;
  unsigned long now = millis();

  // Keep the HTTP OTA endpoint responsive between e-paper refreshes.  The
  // upload is authenticated and a reboot is performed by the updater only
  // after the image has been fully verified.
  otaServer.handleClient();
  MDNS.update();

  pollBootButton(false, clearQueued);
  if (clearQueued) {
    clearQueued = false;
    if (clearPanel()) {
      // Keep the panel white until the next normal poll.  This makes the
      // release-triggered action observable instead of repainting immediately.
      hasPainted = true;
      hasAttempted = true;
      previous = Quota{};
      lastAttempt = now;
      lastPoll = now;
      first = false;
      return;
    }
  }

  if (!first && now - lastPoll < POLL_MS) { delay(100); return; }
  lastPoll = now;
  ensureWifi();
  if (WiFi.status() == WL_CONNECTED && !otaStarted) {
    startOta();
    otaStarted = true;
  }
  Quota quota = readQuota();
  if (!quota.valid) lastPoll = now - POLL_MS + 30000UL;
  now = millis();
  if (epaperRefreshDue(hasPainted, hasAttempted, now, lastAttempt, MIN_REFRESH_MS, quota, previous)) {
    hasAttempted = true;
    bool painted = drawQuota(quota);
    lastAttempt = millis(); // Minimum interval begins after refresh completes.
    if (painted) { previous = quota; hasPainted = true; }
  }
  first = false;
}
