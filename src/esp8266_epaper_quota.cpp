// Experimental 296x152 tri-color panel profile. Not hardware-validated.
// The manufacturer example and supplied panel specification disagree about BUSY.
// Do not upload until the panel/controller and electrical compatibility are confirmed.
// Display sequence/plane conventions adapted from sym090701's MIT-licensed
// esp8266-e2266fs092-photo-frame; see docs/licenses/esp8266-photo-frame-MIT.txt.
#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WiFi.h>
#include <SPI.h>
#include "xiaomiao_boot_config.h"

namespace {
constexpr uint8_t PIN_BUSY = D1, PIN_RESET = D4, PIN_DC = D2, PIN_CS = D8;
constexpr uint16_t PANEL_WIDTH = 152, PANEL_HEIGHT = 296;
constexpr size_t PLANE_BYTES = PANEL_WIDTH * PANEL_HEIGHT / 8;
constexpr unsigned long POLL_MS = 5UL * 60UL * 1000UL;
constexpr unsigned long REPAINT_MS = 24UL * 60UL * 60UL * 1000UL;
constexpr unsigned long BUSY_TIMEOUT_MS = 60000UL;
#ifndef EPD_BUSY_LEVEL
// Matches the supplied ESP8266 demo, NOT the conflicting panel PDF.
#define EPD_BUSY_LEVEL LOW
#endif
#ifndef EPD_ALLOW_UNVERIFIED_DRIVER
#define EPD_ALLOW_UNVERIFIED_DRIVER 0
#endif
GFXcanvas1 canvas(PANEL_WIDTH, PANEL_HEIGHT);

struct Quota {
  bool valid = false;
  int five = -1;
  int weekly = -1;
};

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

void text(int x, int y, uint8_t size, const String &value) {
  canvas.setCursor(x, y); canvas.setTextSize(size); canvas.setTextColor(0);
  canvas.print(value);
}
void metric(int y, const char *name, int value) {
  text(8, y, 2, name);
  text(196, y, 2, value >= 0 ? String(value) + "%" : "--");
  canvas.drawRect(8, y + 22, 280, 12, 0);
  if (value >= 0) canvas.fillRect(10, y + 24, value * 276 / 100, 8, 0);
}
bool drawQuota(const Quota &quota) {
  if (!canvas.getBuffer()) { Serial.println(F("[EPD] allocation failed")); return false; }
  canvas.setRotation(1); canvas.fillScreen(1); canvas.setTextWrap(false);
  text(8, 5, 2, "CODEX QUOTA");
  text(214, 8, 1, quota.valid ? "ONLINE" : "OFFLINE");
  metric(32, "5 HOURS", quota.five);
  metric(78, "WEEK", quota.weekly);
  text(8, 135, 1, "LAN / 5 MIN POLL");
  return panelDisplay();
}

int remaining(JsonVariantConst window) {
  if (!window.is<JsonObjectConst>() || !window["remaining_percent"].is<float>()) return -1;
  float value = window["remaining_percent"].as<float>();
  if (!isfinite(value) || value < 0 || value > 100) return -1;
  return int(value + 0.5f);
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
      out.five = remaining(doc["five_hour"]);
      out.weekly = remaining(doc["weekly"]);
      out.valid = out.five >= 0 || out.weekly >= 0;
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
}  // namespace

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUSY, INPUT);
  pinMode(PIN_RESET, OUTPUT); pinMode(PIN_DC, OUTPUT); pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH); digitalWrite(PIN_RESET, HIGH);
  SPI.begin(); WiFi.mode(WIFI_STA);
}

void loop() {
  static bool first = true, lastPaintOk = false;
  static unsigned long lastPoll = 0, lastPaint = 0;
  static Quota previous;
  unsigned long now = millis();
  if (!first && now - lastPoll < POLL_MS) { delay(100); return; }
  lastPoll = now;
  ensureWifi();
  Quota quota = readQuota();
  if (!quota.valid) lastPoll = now - POLL_MS + 30000UL;
  bool changed = first || quota.valid != previous.valid || quota.five != previous.five || quota.weekly != previous.weekly;
  if (changed || !lastPaintOk || now - lastPaint >= REPAINT_MS) {
    lastPaintOk = drawQuota(quota);
    if (lastPaintOk) { lastPaint = now; previous = quota; }
  }
  first = false;
}
