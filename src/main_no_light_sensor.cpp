#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <SPI.h>
#include <WebServer.h>
#include <WebSocketsClient.h>
#include <HTTPClient.h>
#include <WiFi.h>
#ifdef XM_PRECONFIGURED
#include "xiaomiao_boot_config.h"
#endif
#ifdef XIAOMIAO
#include <esp_ota_ops.h>
#include "xiaomiao_page.h"
#endif

constexpr int SCREEN_W = 160;
#ifdef XIAOMIAO
constexpr int SCREEN_H = 128;
#else
constexpr int SCREEN_H = 80;
#endif
#ifdef XIAOMIAO
constexpr int PIN_BLK = -1;
constexpr int PIN_CS = 5;
constexpr int PIN_DC = 4;
constexpr int PIN_RST = 19;
constexpr int PIN_SDA = 23;
constexpr int PIN_SCL = 18;
#else
constexpr int PIN_BLK = 5;
constexpr int PIN_CS = 6;
constexpr int PIN_DC = 7;
constexpr int PIN_RST = 9;
constexpr int PIN_SDA = 10;
constexpr int PIN_SCL = 8;
#endif
constexpr char DEVICE_HOSTNAME[] = "codexusage";
constexpr bool DISPLAY_INVERTED = true;
constexpr uint8_t DISPLAY_COL_START = 26;
constexpr uint8_t DISPLAY_ROW_START = 1;
constexpr uint32_t WIFI_RETRY_MS = 15000;
constexpr uint32_t WS_STALE_MS = 12000;
constexpr uint32_t WS_RECONNECT_MS = 5000;
constexpr uint32_t DEVICE_STATUS_MS = 5000;
constexpr uint32_t BACKLIGHT_PWM_HZ = 20000;
constexpr uint8_t BACKLIGHT_PWM_BITS = 8;
constexpr uint8_t BACKLIGHT_MAX_DUTY = 255;
constexpr uint16_t DEFAULT_USAGE_PORT = 8080;
constexpr uint16_t DEFAULT_REFRESH_SECONDS = 30;
constexpr uint32_t FIVE_HOUR_WINDOW_SECONDS = 5UL * 60UL * 60UL;
constexpr uint32_t WEEK_WINDOW_SECONDS = 7UL * 24UL * 60UL * 60UL;
#ifdef XIAOMIAO
constexpr char TREND_HOST[] = "192.168.1.158";
constexpr uint16_t TREND_PORT = 8788;
constexpr uint32_t TREND_FETCH_MS = 60000UL;
#endif

class CalibratedST7735 : public Adafruit_ST7735 {
public:
  using Adafruit_ST7735::Adafruit_ST7735;

  void setPanelOffset(uint8_t colStart, uint8_t rowStart) {
    setColRowStart(colStart, rowStart);
    setRotation(getRotation());
  }
};

CalibratedST7735 tft(PIN_CS, PIN_DC, PIN_RST);
GFXcanvas16 frameA(SCREEN_W, SCREEN_H);
GFXcanvas16 frameB(SCREEN_W, SCREEN_H);
WebServer webServer(80);
WebSocketsClient webSocket;
Preferences prefs;

struct AppConfig {
  String wifiSsid;
  String wifiPass;
  String usageHost = "192.168.1.100";
  uint16_t usagePort = DEFAULT_USAGE_PORT;
  uint16_t refreshSeconds = DEFAULT_REFRESH_SECONDS;
  bool screenFlipped = false;
  bool followHostScreen = true;
  uint8_t backlightPercent = 100;
  uint8_t displayMode = 0;
  uint8_t memoryMetric = 0;
  String performanceBackground = "#080A10";
  String performanceCpuColor = "#37D1B0";
  String performanceMemoryColor = "#77DC57";
  String performanceDiskColor = "#FFC440";
  String performanceLoadColor = "#66E1BD";
  String performanceRxColor = "#37D1B0";
  String performanceTxColor = "#77DC57";
  String codexPermissionColor = "#FFB020";
  String codexWorkingColor = "#4F8CFF";
  String codexStopColor = "#4CE0B3";
  uint8_t codexWorkingAnimation = 1;
};

struct UsageWindow {
  bool valid = false;
  float usedPercent = 0.0f;
  float remainingPercent = 0.0f;
  int32_t resetAfterSeconds = -1;
  int32_t limitWindowSeconds = -1;
};

struct UsageState {
  bool ok = false;
  String error;
  String planType;
  bool limitReached = false;
  UsageWindow fiveHour;
  UsageWindow weekly;
  uint32_t lastFetchMs = 0;
};

#ifdef XIAOMIAO
constexpr uint8_t TREND_POINTS = 48;          // 4 hours at 5-minute samples.
float trendFiveHour[TREND_POINTS] = {};
uint32_t trendEpochs[TREND_POINTS] = {};
uint32_t trendResetEpochs[TREND_POINTS] = {};
char trendTimes[TREND_POINTS][6] = {};
uint8_t trendCount = 0;
uint8_t trendHead = 0;
uint32_t lastTrendFetchMs = 0;
uint32_t trendLastSuccessMs = 0;
uint8_t trendWindowHours = 12;
uint8_t trendCursor = 0;
float trendRatePerHour = 0.0f;
float trendEtaHours = -1.0f;
float trendFreshnessSeconds = -1.0f;
int32_t trendLatestResetSeconds = -1;
#endif

struct PerformanceState {
  bool ok = false;
  String error;
  String hostName;
  float cpuPercent = 0.0f;
  float memoryPercent = 0.0f;
  float memoryPressurePercent = 0.0f;
  float diskPercent = 0.0f;
  float loadPercent = 0.0f;
  float netRxMbps = 0.0f;
  float netTxMbps = 0.0f;
  float displayCpu = 0.0f;
  float displayMemory = 0.0f;
  float displayDisk = 0.0f;
  float displayLoad = 0.0f;
  uint32_t uptimeSeconds = 0;
  uint32_t lastFetchMs = 0;
};

struct CodexState {
  String state = "stopped";
};

AppConfig config;
UsageState usage;
PerformanceState performance;
CodexState codex;
String apName;
String apPassword;
uint32_t lastWifiAttemptMs = 0;
uint32_t lastRenderMs = 0;
uint32_t lastWsMessageMs = 0;
uint32_t lastDeviceStatusMs = 0;
bool usagePageDirty = true;
bool usagePageReadyCached = false;
bool mdnsStarted = false;
bool wsStarted = false;
volatile bool hostScreenOn = true;
bool wsConnected = false;
#ifdef XM_STANDALONE_OTA
#include "xiaomiao_ota.h"
bool showOtaInfo = false;
#endif
#ifdef XIAOMIAO
uint32_t footerUntilMs = 0;
#endif
float backlightDuty = BACKLIGHT_MAX_DUTY;
bool backlightPwmReady = false;

void webSocketEvent(WStype_t type, uint8_t *payload, size_t length);

void writeBacklight(uint8_t duty) {
#ifdef XIAOMIAO
  // The original XiaoMiao panel has no software-controlled backlight pin.
  (void)duty;
  return;
#endif
  if (backlightPwmReady) {
    ledcWrite(PIN_BLK, duty);
  } else {
    digitalWrite(PIN_BLK, duty > 0 ? HIGH : LOW);
  }
}

void updateBacklight() {
  const bool screenOn = !config.followHostScreen || hostScreenOn;
  backlightDuty = screenOn
                      ? roundf(BACKLIGHT_MAX_DUTY * config.backlightPercent / 100.0f)
                      : 0.0f;
  writeBacklight(uint8_t(backlightDuty));
}

void applyScreenDirection() {
#ifdef XIAOMIAO
  tft.setRotation(config.screenFlipped ? 1 : 3);
#else
  tft.setRotation(config.screenFlipped ? 3 : 1);
#endif
}

uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

bool isHexColor(const String &value) {
  if (value.length() != 7 || value[0] != '#') {
    return false;
  }
  for (uint8_t i = 1; i < 7; i++) {
    char c = value[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
          (c >= 'A' && c <= 'F'))) {
      return false;
    }
  }
  return true;
}

String normalizedColor(String value, const String &fallback) {
  if (!isHexColor(value)) {
    return fallback;
  }
  value.toUpperCase();
  return value;
}

uint16_t color565(const String &value) {
  uint32_t rgb = strtoul(value.c_str() + 1, nullptr, 16);
  return rgb565((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
}

float clampPercent(float value) {
  if (isnan(value)) {
    return 0.0f;
  }
  return constrain(value, 0.0f, 100.0f);
}

String shortDuration(int32_t seconds) {
  if (seconds < 0) {
    return "--";
  }
  uint32_t s = seconds;
  uint32_t d = s / 86400UL;
  uint32_t h = (s % 86400UL) / 3600UL;
  uint32_t m = (s % 3600UL) / 60UL;

  if (d > 0) {
    return String(d) + "d" + String(h) + "h";
  }
  if (h > 0) {
    return String(h) + "h" + String(m) + "m";
  }
  return String(m) + "m";
}

String percentText(float value) {
  return String(int(roundf(clampPercent(value)))) + "%";
}

float resetProgress(const UsageWindow &window, uint32_t fallbackWindowSeconds) {
  if (window.resetAfterSeconds < 0) {
    return 0.0f;
  }

  uint32_t total = window.limitWindowSeconds > 0 ? window.limitWindowSeconds : fallbackWindowSeconds;
  return constrain(1.0f - float(window.resetAfterSeconds) / float(total), 0.0f, 1.0f);
}

void saveConfig();

void loadConfig() {
  prefs.begin("codex", true);
  config.wifiSsid = prefs.getString("ssid", "");
  config.wifiPass = prefs.getString("pass", "");
  config.usageHost = prefs.getString("host", config.usageHost);
  config.usagePort = prefs.getUShort("port", DEFAULT_USAGE_PORT);
  config.refreshSeconds = constrain(prefs.getUShort("refresh", DEFAULT_REFRESH_SECONDS), 1, 3600);
  config.screenFlipped = prefs.getBool("flip", false);
  config.followHostScreen = prefs.getBool("follow", true);
  config.backlightPercent = constrain(prefs.getUChar("brightness", 100), 1, 100);
  config.displayMode = constrain(prefs.getUChar("mode", 0), 0, 2);
  config.memoryMetric = constrain(prefs.getUChar("memmetric", 0), 0, 1);
  config.performanceBackground = normalizedColor(
      prefs.getString("perf_bg", config.performanceBackground), config.performanceBackground);
  config.performanceCpuColor = normalizedColor(
      prefs.getString("perf_cpu", config.performanceCpuColor), config.performanceCpuColor);
  config.performanceMemoryColor = normalizedColor(
      prefs.getString("perf_mem", config.performanceMemoryColor), config.performanceMemoryColor);
  config.performanceDiskColor = normalizedColor(
      prefs.getString("perf_disk", config.performanceDiskColor), config.performanceDiskColor);
  config.performanceLoadColor = normalizedColor(
      prefs.getString("perf_load", config.performanceLoadColor), config.performanceLoadColor);
  config.performanceRxColor = normalizedColor(
      prefs.getString("perf_rx", config.performanceRxColor), config.performanceRxColor);
  config.performanceTxColor = normalizedColor(
      prefs.getString("perf_tx", config.performanceTxColor), config.performanceTxColor);
  config.codexPermissionColor = normalizedColor(
      prefs.getString("cx_perm", config.codexPermissionColor), config.codexPermissionColor);
  config.codexWorkingColor = normalizedColor(
      prefs.getString("cx_work", config.codexWorkingColor), config.codexWorkingColor);
  config.codexStopColor = normalizedColor(
      prefs.getString("cx_stop", config.codexStopColor), config.codexStopColor);
  config.codexWorkingAnimation = prefs.getUChar("cx_anim", 1) == 2 ? 2 : 1;
  prefs.end();
#ifdef XM_PRECONFIGURED
  // Seed only when compiled network settings change. Web edits remain usable.
  prefs.begin("xm_boot", true);
  bool needsSeed = prefs.getString("revision", "") != XM_BOOT_REV;
  prefs.end();
  if (needsSeed) {
    config.wifiSsid = XM_NETWORKS[0].ssid;
    config.wifiPass = XM_NETWORKS[0].password;
    config.usageHost = XM_BOOT_HOST;
    config.usagePort = XM_BOOT_PORT;
    saveConfig();
    prefs.begin("xm_boot", false);
    prefs.putString("revision", XM_BOOT_REV);
    prefs.end();
  }
#endif
}

void saveConfig() {
  prefs.begin("codex", false);
  prefs.putString("ssid", config.wifiSsid);
  prefs.putString("pass", config.wifiPass);
  prefs.putString("host", config.usageHost);
  prefs.putUShort("port", config.usagePort);
  prefs.putUShort("refresh", config.refreshSeconds);
  prefs.putBool("flip", config.screenFlipped);
  prefs.putBool("follow", config.followHostScreen);
  prefs.putUChar("brightness", config.backlightPercent);
  prefs.putUChar("mode", config.displayMode);
  prefs.putUChar("memmetric", config.memoryMetric);
  prefs.putString("perf_bg", config.performanceBackground);
  prefs.putString("perf_cpu", config.performanceCpuColor);
  prefs.putString("perf_mem", config.performanceMemoryColor);
  prefs.putString("perf_disk", config.performanceDiskColor);
  prefs.putString("perf_load", config.performanceLoadColor);
  prefs.putString("perf_rx", config.performanceRxColor);
  prefs.putString("perf_tx", config.performanceTxColor);
  prefs.putString("cx_perm", config.codexPermissionColor);
  prefs.putString("cx_work", config.codexWorkingColor);
  prefs.putString("cx_stop", config.codexStopColor);
  prefs.putUChar("cx_anim", config.codexWorkingAnimation);
  prefs.end();
}

String usageUrl() {
  return String("http://") + config.usageHost + ":" + String(config.usagePort) + "/usage";
}

String performanceUrl() {
  return String("http://") + config.usageHost + ":" + String(config.usagePort) + "/performance";
}

String screenStateUrl() {
  return String("http://") + config.usageHost + ":" + String(config.usagePort) + "/screen";
}

void handleRoot() {
#ifdef XIAOMIAO
  webServer.send_P(200, "text/html; charset=utf-8", XM_PAGE);
  return;
#endif
  File page = LittleFS.open("/index.html", "r");
  if (!page) {
    webServer.send(500, "text/plain; charset=utf-8", "配置页面文件缺失");
    return;
  }
  webServer.streamFile(page, "text/html; charset=utf-8");
  page.close();
}

void handleConfigApi() {
  StaticJsonDocument<1536> doc;
  doc["ssid"] = config.wifiSsid;
  doc["pass"] = config.wifiPass;
  doc["host"] = config.usageHost;
  doc["port"] = config.usagePort;
  doc["refresh_seconds"] = config.refreshSeconds;
  doc["screen_flipped"] = config.screenFlipped;
  doc["follow_host_screen"] = config.followHostScreen;
  doc["has_light_sensor"] = false;
  doc["backlight_brightness"] = config.backlightPercent;
  doc["display_mode"] = config.displayMode;
  doc["memory_metric"] = config.memoryMetric;
  doc["performance_background"] = config.performanceBackground;
  doc["performance_cpu_color"] = config.performanceCpuColor;
  doc["performance_memory_color"] = config.performanceMemoryColor;
  doc["performance_disk_color"] = config.performanceDiskColor;
  doc["performance_load_color"] = config.performanceLoadColor;
  doc["performance_rx_color"] = config.performanceRxColor;
  doc["performance_tx_color"] = config.performanceTxColor;
  doc["codex_permission_color"] = config.codexPermissionColor;
  doc["codex_working_color"] = config.codexWorkingColor;
  doc["codex_stop_color"] = config.codexStopColor;
  doc["codex_working_animation"] = config.codexWorkingAnimation;
  doc["ap_name"] = apName;
  doc["ap_ip"] = WiFi.softAPIP().toString();
  doc["connected"] = WiFi.status() == WL_CONNECTED;
  doc["local_ip"] = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "";
  doc["usage_url"] = usageUrl();
  doc["performance_url"] = performanceUrl();
  doc["screen_state_url"] = screenStateUrl();
  doc["websocket_url"] = String("ws://") + config.usageHost + ":" +
                         String(config.usagePort) + "/ws";
  doc["host_screen_on"] = bool(hostScreenOn);
  doc["backlight_percent"] = int(roundf(backlightDuty * 100.0f / 255.0f));
  doc["websocket_connected"] = wsConnected;

  String response;
  serializeJson(doc, response);
  webServer.send(200, "application/json; charset=utf-8", response);
}

void handleSave() {
  config.wifiSsid = webServer.arg("ssid");
  config.wifiPass = webServer.arg("pass");
  config.usageHost = webServer.arg("host");
  config.usagePort = constrain(webServer.arg("port").toInt(), 1, 65535);
  config.refreshSeconds = constrain(webServer.arg("refresh_seconds").toInt(), 1, 3600);
  config.screenFlipped = webServer.arg("screen_direction") == "flipped";
  if (webServer.hasArg("backlight_brightness")) {
    config.backlightPercent =
        constrain(webServer.arg("backlight_brightness").toInt(), 1, 100);
  }
  config.displayMode = constrain(webServer.arg("display_mode").toInt(), 0, 2);
  config.memoryMetric = constrain(webServer.arg("memory_metric").toInt(), 0, 1);
  config.performanceBackground = normalizedColor(
      webServer.arg("performance_background"), config.performanceBackground);
  config.performanceCpuColor = normalizedColor(
      webServer.arg("performance_cpu_color"), config.performanceCpuColor);
  config.performanceMemoryColor = normalizedColor(
      webServer.arg("performance_memory_color"), config.performanceMemoryColor);
  config.performanceDiskColor = normalizedColor(
      webServer.arg("performance_disk_color"), config.performanceDiskColor);
  config.performanceLoadColor = normalizedColor(
      webServer.arg("performance_load_color"), config.performanceLoadColor);
  config.performanceRxColor = normalizedColor(
      webServer.arg("performance_rx_color"), config.performanceRxColor);
  config.performanceTxColor = normalizedColor(
      webServer.arg("performance_tx_color"), config.performanceTxColor);
  config.codexPermissionColor = normalizedColor(
      webServer.arg("codex_permission_color"), config.codexPermissionColor);
  config.codexWorkingColor = normalizedColor(
      webServer.arg("codex_working_color"), config.codexWorkingColor);
  config.codexStopColor = normalizedColor(
      webServer.arg("codex_stop_color"), config.codexStopColor);
  if (webServer.hasArg("codex_working_animation")) {
    config.codexWorkingAnimation =
        webServer.arg("codex_working_animation").toInt() == 2 ? 2 : 1;
  }
  saveConfig();
  updateBacklight();
  webServer.send(200, "application/json; charset=utf-8", "{\"ok\":true}");
  applyScreenDirection();
  WiFi.disconnect(false);
  webSocket.disconnect();
  wsStarted = false;
  wsConnected = false;
  lastWifiAttemptMs = 0;
  usage.lastFetchMs = 0;
  usage.ok = false;
  performance.lastFetchMs = 0;
  performance.ok = false;
}

void setupWebServer() {
#ifdef XM_STANDALONE_OTA
  xmSetupOta();
#endif
  webServer.on("/", HTTP_GET, handleRoot);
  webServer.on("/api/config", HTTP_GET, handleConfigApi);
  webServer.on("/save", HTTP_POST, handleSave);
  webServer.onNotFound([]() {
    webServer.sendHeader("Location", "/", true);
    webServer.send(302, "text/plain", "");
  });
  webServer.begin();
}

void connectWifiIfNeeded() {
  if (!config.wifiSsid.length() || WiFi.status() == WL_CONNECTED) {
    return;
  }

  uint32_t now = millis();
  if (lastWifiAttemptMs != 0 && now - lastWifiAttemptMs < WIFI_RETRY_MS) {
    return;
  }

  lastWifiAttemptMs = now;
#ifdef XM_PRECONFIGURED
  // Only use the compiled list while the saved SSID belongs to that list.
  // A different SSID saved in the portal takes precedence.
  bool compiledNetwork = false;
  for (unsigned i = 0; i < XM_NETWORK_COUNT; ++i) {
    if (config.wifiSsid == XM_NETWORKS[i].ssid) compiledNetwork = true;
  }
  if (compiledNetwork) {
    static unsigned nextNetwork = 0;
    const XmNetwork &network = XM_NETWORKS[nextNetwork++ % XM_NETWORK_COUNT];
    WiFi.begin(network.ssid, network.password);
    return;
  }
#endif
  WiFi.begin(config.wifiSsid.c_str(), config.wifiPass.c_str());
}

void setupNetwork() {
  uint64_t chipId = ESP.getEfuseMac();
  apName = "CodexUsage-" + String(uint16_t(chipId), HEX);
#ifdef XIAOMIAO
  prefs.begin("xm_setup", false);
  apPassword = prefs.getString("ap_pass", "");
  if (apPassword.length() < 8) {
    apPassword = "XM-" + String(uint32_t(esp_random()), HEX) + "-setup";
    prefs.putString("ap_pass", apPassword);
  }
  prefs.end();
#endif
  WiFi.setHostname(DEVICE_HOSTNAME);
  WiFi.mode(WIFI_AP_STA);
#ifdef XIAOMIAO
  WiFi.softAP(apName.c_str(), apPassword.c_str());
#else
  WiFi.softAP(apName.c_str());
#endif
  connectWifiIfNeeded();
  setupWebServer();
}

void setupMdnsIfNeeded() {
  if (mdnsStarted || WiFi.status() != WL_CONNECTED) {
    return;
  }
  if (MDNS.begin(DEVICE_HOSTNAME)) {
    MDNS.addService("http", "tcp", 80);
#ifdef XM_STANDALONE_OTA
    MDNS.setInstanceName("XiaoMiao Codex OTA");
    MDNS.addService("xiaomiao-ota", "tcp", 80);
    MDNS.addServiceTxt("xiaomiao-ota", "tcp", "target", "xiaomiao-esp32");
    MDNS.addServiceTxt("xiaomiao-ota", "tcp", "path", "/ota");
    MDNS.addServiceTxt("xiaomiao-ota", "tcp", "auth", "http-digest");
    MDNS.addServiceTxt("xiaomiao-ota", "tcp", "protocol", "1");
    Serial.println("[mDNS] codexusage.local / _xiaomiao-ota._tcp");
#endif
    mdnsStarted = true;
  }
}

void setupWebSocketIfNeeded() {
  if (wsStarted || WiFi.status() != WL_CONNECTED || !config.usageHost.length()) {
    return;
  }
  String path = String("/ws?device=") + DEVICE_HOSTNAME;
  webSocket.begin(config.usageHost, config.usagePort, path);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(WS_RECONNECT_MS);
  webSocket.enableHeartbeat(15000, 3000, 2);
  wsStarted = true;
}

UsageWindow parseUsageWindow(JsonVariantConst window) {
  UsageWindow out;
  if (window.isNull()) {
    return out;
  }

  if (!window["used_percent"].isNull()) {
    out.usedPercent = clampPercent(window["used_percent"].as<float>());
    out.remainingPercent = clampPercent(100.0f - out.usedPercent);
    out.valid = true;
  }
  if (!window["remaining_percent"].isNull()) {
    out.remainingPercent = clampPercent(window["remaining_percent"].as<float>());
    out.valid = true;
  }
  if (!window["reset_after_seconds"].isNull()) {
    out.resetAfterSeconds = window["reset_after_seconds"].as<int32_t>();
  }
  if (!window["limit_window_seconds"].isNull()) {
    out.limitWindowSeconds = window["limit_window_seconds"].as<int32_t>();
  }
  return out;
}

void applyUsagePayload(JsonVariantConst doc) {
#ifdef XIAOMIAO
  bool wasOk = usage.ok;
#endif
  usage.ok = doc["ok"].as<bool>();
#ifdef XIAOMIAO
  if (usage.ok != wasOk) Serial.printf("[Codex] quota %s\n", usage.ok ? "ready" : "unavailable");
#endif
  usage.error = doc["error"] | "";
  usage.planType = doc["plan_type"] | "";
  usage.limitReached = doc["limit_reached"].as<bool>();
  usage.fiveHour = parseUsageWindow(doc["five_hour"]);
  usage.weekly = parseUsageWindow(doc["weekly"]);
  usagePageDirty = true;
}

void applyPerformancePayload(JsonVariantConst doc) {
  bool firstSample = !performance.ok;
  if (!doc["ok"].as<bool>()) {
    performance.ok = false;
    performance.error = doc["error"] | "server error";
    return;
  }
  performance.ok = true;
  performance.error = "";

  performance.hostName = doc["host_name"] | "HOST";
  performance.cpuPercent = clampPercent(doc["cpu_percent"] | 0.0f);
  performance.memoryPercent = clampPercent(doc["memory_percent"] | 0.0f);
  performance.memoryPressurePercent = clampPercent(doc["memory_pressure_percent"] | 0.0f);
  performance.diskPercent = clampPercent(doc["disk_percent"] | 0.0f);
  float cpuCount = max(1.0f, doc["cpu_count"] | 1.0f);
  performance.loadPercent = clampPercent((doc["load_1"] | 0.0f) * 100.0f / cpuCount);
  performance.netRxMbps = max(0.0f, doc["net_rx_mbps"] | 0.0f);
  performance.netTxMbps = max(0.0f, doc["net_tx_mbps"] | 0.0f);
  performance.uptimeSeconds = doc["uptime_seconds"] | 0UL;

  if (firstSample) {
    performance.displayCpu = performance.cpuPercent;
    performance.displayMemory = config.memoryMetric == 1 ? performance.memoryPressurePercent
                                                          : performance.memoryPercent;
    performance.displayDisk = performance.diskPercent;
    performance.displayLoad = performance.loadPercent;
  }
}

void appendDeviceConfig(JsonObject doc) {
  doc["display_mode"] = config.displayMode;
  doc["memory_metric"] = config.memoryMetric;
  doc["screen_flipped"] = config.screenFlipped;
  doc["follow_host_screen"] = config.followHostScreen;
  doc["has_light_sensor"] = false;
  doc["backlight_brightness"] = config.backlightPercent;
  doc["performance_background"] = config.performanceBackground;
  doc["performance_cpu_color"] = config.performanceCpuColor;
  doc["performance_memory_color"] = config.performanceMemoryColor;
  doc["performance_disk_color"] = config.performanceDiskColor;
  doc["performance_load_color"] = config.performanceLoadColor;
  doc["performance_rx_color"] = config.performanceRxColor;
  doc["performance_tx_color"] = config.performanceTxColor;
  doc["codex_permission_color"] = config.codexPermissionColor;
  doc["codex_working_color"] = config.codexWorkingColor;
  doc["codex_stop_color"] = config.codexStopColor;
  doc["codex_working_animation"] = config.codexWorkingAnimation;
}

void sendDeviceStatus(const char *type) {
  DynamicJsonDocument doc(1536);
  doc["type"] = type;
  doc["device"] = DEVICE_HOSTNAME;
  doc["local_ip"] = WiFi.localIP().toString();
  doc["rssi"] = WiFi.RSSI();
  doc["has_light_sensor"] = false;
  doc["backlight_percent"] = int(roundf(backlightDuty * 100.0f / 255.0f));
  appendDeviceConfig(doc.createNestedObject("config"));
  String message;
  serializeJson(doc, message);
  webSocket.sendTXT(message);
}

void applyRemoteConfig(JsonVariantConst remote) {
  if (remote["display_mode"].is<int>()) {
    config.displayMode = constrain(remote["display_mode"].as<int>(), 0, 2);
  }
  if (remote["memory_metric"].is<int>()) {
    config.memoryMetric = constrain(remote["memory_metric"].as<int>(), 0, 1);
  }
  if (remote["screen_flipped"].is<bool>()) {
    config.screenFlipped = remote["screen_flipped"].as<bool>();
  }
  if (remote["follow_host_screen"].is<bool>()) {
    config.followHostScreen = remote["follow_host_screen"].as<bool>();
  }
  if (remote["backlight_brightness"].is<int>()) {
    config.backlightPercent =
        constrain(remote["backlight_brightness"].as<int>(), 1, 100);
  }
  config.performanceBackground = normalizedColor(
      remote["performance_background"] | config.performanceBackground.c_str(),
      config.performanceBackground);
  config.performanceCpuColor = normalizedColor(
      remote["performance_cpu_color"] | config.performanceCpuColor.c_str(),
      config.performanceCpuColor);
  config.performanceMemoryColor = normalizedColor(
      remote["performance_memory_color"] | config.performanceMemoryColor.c_str(),
      config.performanceMemoryColor);
  config.performanceDiskColor = normalizedColor(
      remote["performance_disk_color"] | config.performanceDiskColor.c_str(),
      config.performanceDiskColor);
  config.performanceLoadColor = normalizedColor(
      remote["performance_load_color"] | config.performanceLoadColor.c_str(),
      config.performanceLoadColor);
  config.performanceRxColor = normalizedColor(
      remote["performance_rx_color"] | config.performanceRxColor.c_str(),
      config.performanceRxColor);
  config.performanceTxColor = normalizedColor(
      remote["performance_tx_color"] | config.performanceTxColor.c_str(),
      config.performanceTxColor);
  config.codexPermissionColor = normalizedColor(
      remote["codex_permission_color"] | config.codexPermissionColor.c_str(),
      config.codexPermissionColor);
  config.codexWorkingColor = normalizedColor(
      remote["codex_working_color"] | config.codexWorkingColor.c_str(),
      config.codexWorkingColor);
  config.codexStopColor = normalizedColor(
      remote["codex_stop_color"] | config.codexStopColor.c_str(),
      config.codexStopColor);
  if (remote["codex_working_animation"].is<int>()) {
    config.codexWorkingAnimation =
        remote["codex_working_animation"].as<int>() == 2 ? 2 : 1;
  }
  saveConfig();
  applyScreenDirection();
  updateBacklight();
  usagePageDirty = true;
  lastRenderMs = 0;
  sendDeviceStatus("config_ack");
}

void handleWsMessage(uint8_t *payload, size_t length) {
  DynamicJsonDocument doc(8192);
  DeserializationError error = deserializeJson(doc, payload, length);
  if (error) {
    return;
  }
  lastWsMessageMs = millis();
  String type = doc["type"] | "";
  if (type == "screen") {
    hostScreenOn = doc["screen_on"] | true;
  } else if (type == "snapshot") {
    hostScreenOn = doc["screen_on"] | true;
    applyUsagePayload(doc["usage"]);
    applyPerformancePayload(doc["performance"]);
    codex.state = doc["codex"]["state"] | "stopped";
  } else if (type == "config") {
    applyRemoteConfig(doc["config"]);
  } else if (type == "codex_status") {
    codex.state = doc["codex"]["state"] | "stopped";
    lastRenderMs = 0;
  }
}

void webSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
#ifdef XIAOMIAO
      Serial.printf("[Codex] WebSocket connected, IP=%s\n", WiFi.localIP().toString().c_str());
#endif
      lastWsMessageMs = millis();
      sendDeviceStatus("hello");
      break;
    case WStype_DISCONNECTED:
      wsConnected = false;
      break;
    case WStype_TEXT:
      handleWsMessage(payload, length);
      break;
    case WStype_PONG:
      lastWsMessageMs = millis();
      break;
    default:
      break;
  }
}

void updateWsFailSafe() {
  if (config.followHostScreen &&
      ((!wsConnected && millis() > WS_STALE_MS) ||
       (lastWsMessageMs != 0 && millis() - lastWsMessageMs > WS_STALE_MS))) {
    hostScreenOn = false;
  }
}

void drawText(GFXcanvas16 &spr, int16_t x, int16_t y, const String &text, uint16_t color,
              uint8_t size = 1) {
  spr.setTextSize(size);
  spr.setTextColor(color);
  spr.setCursor(x, y);
  spr.print(text);
  spr.setTextSize(1);
}

void drawRightText(GFXcanvas16 &spr, int16_t right, int16_t y, const String &text,
                   uint16_t color, uint8_t size = 1) {
  int16_t width = int16_t(text.length()) * 6 * size;
  drawText(spr, right - width, y, text, color, size);
}

uint16_t blendColor565(uint16_t background, uint16_t foreground, uint8_t coverage) {
  coverage = min(uint8_t(4), coverage);
  uint8_t inverse = 4 - coverage;
  uint8_t r = ((((background >> 11) & 0x1F) * inverse) +
               (((foreground >> 11) & 0x1F) * coverage) + 2) / 4;
  uint8_t g = ((((background >> 5) & 0x3F) * inverse) +
               (((foreground >> 5) & 0x3F) * coverage) + 2) / 4;
  uint8_t b = (((background & 0x1F) * inverse) +
               ((foreground & 0x1F) * coverage) + 2) / 4;
  return (uint16_t(r) << 11) | (uint16_t(g) << 5) | b;
}

void drawRingSegment(GFXcanvas16 &spr, int16_t cx, int16_t cy, int16_t outerRadius,
                     int16_t thickness, float percent, uint16_t color) {
  percent = constrain(percent, 0.0f, 1.0f);
  if (percent <= 0.0f) {
    return;
  }

  int16_t innerRadius = outerRadius - thickness;
  float sweep = 360.0f * percent;
  constexpr float sampleOffsets[2] = {-0.25f, 0.25f};

  for (int16_t y = cy - outerRadius; y <= cy + outerRadius; y++) {
    for (int16_t x = cx - outerRadius; x <= cx + outerRadius; x++) {
      uint8_t coverage = 0;
      for (uint8_t sampleY = 0; sampleY < 2; sampleY++) {
        for (uint8_t sampleX = 0; sampleX < 2; sampleX++) {
          float dx = float(x - cx) + sampleOffsets[sampleX];
          float dy = float(y - cy) + sampleOffsets[sampleY];
          float distanceSquared = dx * dx + dy * dy;
          if (distanceSquared < float(innerRadius * innerRadius) ||
              distanceSquared > float(outerRadius * outerRadius)) {
            continue;
          }
          if (percent < 1.0f) {
            float degrees = atan2f(dy, dx) * 180.0f / PI + 90.0f;
            if (degrees < 0.0f) {
              degrees += 360.0f;
            }
            if (degrees > sweep) {
              continue;
            }
          }
          coverage++;
        }
      }
      if (coverage > 0) {
        spr.drawPixel(x, y, blendColor565(spr.getPixel(x, y), color, coverage));
      }
    }
  }
}

void drawRing(GFXcanvas16 &spr, int16_t cx, int16_t cy, int16_t outerRadius,
              int16_t thickness, float percent, uint16_t trackColor, uint16_t fillColor) {
  drawRingSegment(spr, cx, cy, outerRadius, thickness, 1.0f, trackColor);
  drawRingSegment(spr, cx, cy, outerRadius, thickness, percent, fillColor);
}

void drawProgressBar(GFXcanvas16 &spr, int16_t x, int16_t y, int16_t w, int16_t h,
                     float progress, uint16_t fillColor) {
  progress = constrain(progress, 0.0f, 1.0f);
  spr.drawRect(x, y, w, h, rgb565(47, 56, 78));
  int16_t fillW = int16_t((w - 2) * progress);
  if (fillW > 0) {
    spr.fillRect(x + 1, y + 1, fillW, h - 2, fillColor);
  }
}

uint16_t scaleColor565(uint16_t color, float brightness) {
  brightness = constrain(brightness, 0.0f, 1.0f);
  uint8_t r = uint8_t(((color >> 11) & 0x1F) * brightness);
  uint8_t g = uint8_t(((color >> 5) & 0x3F) * brightness);
  uint8_t b = uint8_t((color & 0x1F) * brightness);
  return (uint16_t(r) << 11) | (uint16_t(g) << 5) | b;
}

uint16_t colorWheel565(uint8_t position) {
  if (position < 85) {
    return rgb565(255 - position * 3, position * 3, 0);
  }
  if (position < 170) {
    position -= 85;
    return rgb565(0, 255 - position * 3, position * 3);
  }
  position -= 170;
  return rgb565(position * 3, 0, 255 - position * 3);
}

void drawCodexStatusIndicator(GFXcanvas16 &spr, int16_t x, int16_t y,
                              int16_t length, bool vertical,
                              bool hideWhenStopped) {
  if (hideWhenStopped && codex.state == "stopped") {
    return;
  }

  String selected = config.codexStopColor;
  float brightness = 1.0f;
  if (codex.state == "permission") {
    selected = config.codexPermissionColor;
    float wave = (sinf(float(millis() % 1600UL) * 2.0f * PI / 1600.0f) + 1.0f) * 0.5f;
    brightness = 0.12f + wave * 0.88f;
  } else if (codex.state == "working") {
    selected = config.codexWorkingColor;
  }

  uint16_t color = color565(selected);
  if (vertical) {
    spr.fillRect(x, y, 4, length, scaleColor565(color, 0.07f));
  } else {
    spr.fillRect(x, y, length, 4, scaleColor565(color, 0.07f));
  }

  int16_t innerLength = length - 2;
  if (codex.state != "working") {
    if (vertical) {
      spr.fillRect(x + 1, y + 1, 3, innerLength, scaleColor565(color, brightness));
    } else {
      spr.fillRect(x + 1, y + 1, innerLength, 3, scaleColor565(color, brightness));
    }
    return;
  }

  if (config.codexWorkingAnimation == 2) {
    uint8_t offset = uint8_t((millis() / 8UL) & 0xFF);
    for (int16_t i = 0; i < innerLength; i++) {
      uint8_t hue = uint8_t(offset + uint16_t(i) * 256 / innerLength);
      if (vertical) {
        spr.drawFastHLine(x + 1, y + 1 + i, 3, colorWheel565(hue));
      } else {
        spr.drawFastVLine(x + 1 + i, y + 1, 3, colorWheel565(hue));
      }
    }
    return;
  }

  constexpr uint32_t shimmerPeriodMs = 1400;
  int16_t trailLength = max(int16_t(8), int16_t(innerLength * 3 / 10));
  int16_t head = int16_t((millis() % shimmerPeriodMs) * innerLength /
                         shimmerPeriodMs);
  for (int16_t i = 0; i < innerLength; i++) {
    int16_t trailDistance = (head - i + innerLength) % innerLength;
    float shimmer = trailDistance <= trailLength
                        ? 1.0f - float(trailDistance) / trailLength
                        : 0.0f;
    uint16_t sliceColor = scaleColor565(color, 0.12f + shimmer * 0.88f);
    if (vertical) {
      spr.drawFastHLine(x + 1, y + 1 + i, 3, sliceColor);
    } else {
      spr.drawFastVLine(x + 1 + i, y + 1, 3, sliceColor);
    }
  }
}

void drawCodexStatusBar(GFXcanvas16 &spr) {
#ifdef XIAOMIAO
  // Match the performance page: an idle Codex must not leave three bright
  // green columns at the panel edge. Active/permission feedback remains.
  drawCodexStatusIndicator(spr, SCREEN_W - 4, 0, SCREEN_H, true, true);
#else
  drawCodexStatusIndicator(spr, SCREEN_W - 4, 0, SCREEN_H, true, false);
#endif
}

void drawStatusPage(GFXcanvas16 &spr, const String &title, const String &line1,
                    const String &line2) {
  spr.fillScreen(rgb565(8, 10, 16));
  spr.drawRect(0, 0, SCREEN_W, SCREEN_H, rgb565(31, 43, 62));
  drawText(spr, 7, 8, title, rgb565(102, 225, 189));
  drawText(spr, 7, 26, line1, ST77XX_WHITE);
  drawText(spr, 7, 40, line2, rgb565(170, 180, 200));
  drawText(spr, 7, 64, "AP " + apName, rgb565(115, 140, 175));
}

void drawUsagePage(GFXcanvas16 &spr) {
  spr.fillScreen(rgb565(8, 10, 16));

  if (!config.wifiSsid.length()) {
    drawStatusPage(spr, "SETUP", "Open 192.168.4.1", "PASS " + apPassword);
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    drawStatusPage(spr, "WIFI...", config.wifiSsid, WiFi.softAPIP().toString());
    return;
  }

  if (!usage.ok) {
    drawStatusPage(spr, "NO DATA", usage.error.length() ? usage.error : "fetching",
                   WiFi.localIP().toString());
    return;
  }

  uint16_t cyan = rgb565(55, 209, 176);
  uint16_t green = rgb565(119, 220, 87);
  uint16_t amber = rgb565(255, 196, 64);
  uint16_t red = rgb565(255, 91, 91);
  uint16_t muted = rgb565(128, 141, 166);
  uint16_t panel = rgb565(18, 23, 34);
  uint16_t text = rgb565(235, 241, 255);

  spr.fillRect(0, 0, SCREEN_W, 11, panel);
  drawText(spr, 4, 2, "CODEX", cyan);
  drawText(spr, 50, 2, usage.planType.length() ? usage.planType : "USAGE", muted);
  drawRightText(spr, SCREEN_W - 2, 2, WiFi.RSSI() > -67 ? "WiFi" : "wifi",
                WiFi.RSSI() > -67 ? green : amber);

  float fiveLeft = usage.fiveHour.valid ? usage.fiveHour.remainingPercent : 0.0f;
  float weekLeft = usage.weekly.valid ? usage.weekly.remainingPercent : 0.0f;
  uint16_t fiveColor = fiveLeft > 35.0f ? cyan : (fiveLeft > 15.0f ? amber : red);
  uint16_t weekColor = weekLeft > 35.0f ? green : (weekLeft > 15.0f ? amber : red);
  uint16_t outerTrack = rgb565(30, 36, 49);
  uint16_t innerTrack = rgb565(40, 48, 64);

#ifdef XIAOMIAO
  drawText(spr, 5, 19, "5 HOUR LEFT", muted);
  drawText(spr, 108, 17, usage.fiveHour.valid ? percentText(fiveLeft) : "--", fiveColor, 2);
  drawProgressBar(spr, 5, 37, 150, 10, fiveLeft / 100.0f, fiveColor);
  drawText(spr, 5, 51, "Reset " + shortDuration(usage.fiveHour.resetAfterSeconds), text);
  drawText(spr, 5, 69, "WEEK LEFT", muted);
  drawText(spr, 108, 67, usage.weekly.valid ? percentText(weekLeft) : "--", weekColor, 2);
  drawProgressBar(spr, 5, 87, 150, 10, weekLeft / 100.0f, weekColor);
  drawText(spr, 5, 101, "Reset " + shortDuration(usage.weekly.resetAfterSeconds), text);
  return;
#endif

  constexpr int16_t ringX = 38;
  constexpr int16_t ringY = 44;
  drawRing(spr, ringX, ringY, 25, 3, weekLeft / 100.0f, outerTrack, weekColor);
  drawRing(spr, ringX, ringY, 18, 4, fiveLeft / 100.0f, innerTrack, fiveColor);
  String fiveText = percentText(fiveLeft);
  drawText(spr, ringX - int16_t(fiveText.length() * 3), ringY - 3, fiveText, fiveColor);

  constexpr int16_t dotX = 73;
  constexpr int16_t labelX = 79;
  constexpr int16_t valueX = 120;
  spr.fillCircle(dotX, 19, 2, fiveColor);
  drawText(spr, labelX, 16, "5H", muted);
  spr.fillCircle(dotX, 32, 2, weekColor);
  drawText(spr, labelX, 29, "WEEK", muted);
  drawText(spr, valueX, 29, percentText(weekLeft), weekColor);
  drawText(spr, labelX, 42, "5H RST", muted);
  drawText(spr, valueX, 42, shortDuration(usage.fiveHour.resetAfterSeconds), text);
  drawText(spr, labelX, 55, "WK RST", muted);
  drawText(spr, valueX, 55, shortDuration(usage.weekly.resetAfterSeconds), text);
  drawProgressBar(spr, 4, 75, 70, 5,
                  resetProgress(usage.fiveHour, FIVE_HOUR_WINDOW_SECONDS), fiveColor);
  drawProgressBar(spr, 80, 75, 68, 5,
                  resetProgress(usage.weekly, WEEK_WINDOW_SECONDS), weekColor);
}

#ifdef XIAOMIAO
void fetchNasTrendIfNeeded() {
  if (config.displayMode != 2 || WiFi.status() != WL_CONNECTED ||
      (lastTrendFetchMs && millis() - lastTrendFetchMs < TREND_FETCH_MS)) {
    return;
  }
  lastTrendFetchMs = millis();
  HTTPClient http;
  String url = String("http://") + TREND_HOST + ":" + String(TREND_PORT) +
               "/api/codex-trend?hours=" + String(trendWindowHours) + "&points=48";
  http.begin(url);
  http.setTimeout(3500);
  int status = http.GET();
  if (status == HTTP_CODE_OK) {
    DynamicJsonDocument doc(12288);
    if (deserializeJson(doc, http.getString()) == DeserializationError::Ok) {
      JsonArray points = doc["points"].as<JsonArray>();
      uint8_t count = min(uint8_t(points.size()), TREND_POINTS);
      trendRatePerHour = doc["rate_percent_per_hour"] | 0.0f;
      trendEtaHours = doc["eta_hours"] | -1.0f;
      trendLatestResetSeconds = doc["latest_reset_after_seconds"] | -1;
      if (count > 0) {
        trendCount = count;
        trendHead = count % TREND_POINTS;
        uint8_t i = 0;
        for (JsonObject point : points) {
          if (i >= count) break;
          trendFiveHour[i] = clampPercent(point["five_hour_remaining"] | 0.0f);
          trendEpochs[i] = point["observed_at_epoch"] | 0UL;
          trendResetEpochs[i] = point["five_hour_reset_at_epoch"] | 0UL;
          String observedAt = point["observed_at_local"] | "";
          if (observedAt.length() >= 16) {
            observedAt.substring(11, 16).toCharArray(trendTimes[i], sizeof(trendTimes[i]));
          } else {
            trendTimes[i][0] = '\0';
          }
          i++;
        }
        trendCursor = count - 1;
        trendLastSuccessMs = millis();
      }
    }
  }
  http.end();
}

void drawTrendGraph(GFXcanvas16 &spr, int16_t x, int16_t y, int16_t w, int16_t h,
                    const String &label, const float *values, uint16_t color) {
  uint16_t panel = rgb565(18, 23, 34);
  uint16_t grid = rgb565(43, 50, 68);
  uint16_t muted = rgb565(128, 141, 166);
  spr.fillRect(x, y, w, h, panel);
  spr.drawRect(x, y, w, h, grid);
  spr.drawFastHLine(x + 22, y + h / 2, w - 23, grid);
  drawText(spr, x + 4, y + 3, label, rgb565(160, 172, 195));
  drawText(spr, x + 2, y + 16, "100", muted);
  drawText(spr, x + 2, y + 44, "50", muted);
  drawText(spr, x + 2, y + 70, "0", muted);
  if (trendCount < 2) {
    drawText(spr, x + w - 42, y + 3, "WAIT", muted);
    return;
  }
  uint8_t oldest = trendCount < TREND_POINTS ? 0 : trendHead;
  int16_t graphX = x + 22;
  int16_t graphY = y + 16;
  int16_t graphW = w - 26;
  int16_t graphH = h - 36;
  constexpr uint8_t axisTicks = 4;
  for (uint8_t tick = 0; tick < axisTicks; tick++) {
    uint8_t sample = uint8_t((uint32_t(tick) * (trendCount - 1)) / (axisTicks - 1));
    int16_t tickX = graphX + int32_t(sample) * graphW / (trendCount - 1);
    spr.drawFastVLine(tickX, graphY, graphH, grid);
    uint8_t index = (oldest + sample) % TREND_POINTS;
    if (trendTimes[index][0]) {
      int16_t labelX = tickX - 15;
      if (labelX < x + 1) labelX = x + 1;
      if (labelX > x + w - 31) labelX = x + w - 31;
      drawText(spr, labelX, y + h - 13, trendTimes[index], muted);
    }
  }
  for (uint8_t i = 1; i < trendCount; i++) {
    uint8_t prev = (oldest + i - 1) % TREND_POINTS;
    uint8_t current = (oldest + i) % TREND_POINTS;
    int16_t x1 = graphX + int32_t(i - 1) * graphW / (trendCount - 1);
    int16_t x2 = graphX + int32_t(i) * graphW / (trendCount - 1);
    int16_t y1 = graphY + graphH - 1 - int16_t(constrain(values[prev], 0.0f, 100.0f) * (graphH - 1) / 100.0f);
    int16_t y2 = graphY + graphH - 1 - int16_t(constrain(values[current], 0.0f, 100.0f) * (graphH - 1) / 100.0f);
    spr.drawLine(x1, y1, x2, y2, color);
  }
  uint32_t firstEpoch = trendEpochs[oldest];
  uint32_t lastEpoch = trendEpochs[(oldest + trendCount - 1) % TREND_POINTS];
  if (lastEpoch > firstEpoch) {
    for (uint8_t i = 0; i < trendCount; i++) {
      uint8_t index = (oldest + i) % TREND_POINTS;
      uint32_t resetEpoch = trendResetEpochs[index];
      if (resetEpoch >= firstEpoch && resetEpoch <= lastEpoch) {
        int16_t resetX = graphX + int32_t(resetEpoch - firstEpoch) * graphW /
                         int32_t(lastEpoch - firstEpoch);
        spr.drawFastVLine(resetX, graphY, graphH, rgb565(255, 120, 80));
        drawText(spr, min<int16_t>(x + w - 10, resetX + 2), graphY, "R", rgb565(255, 120, 80));
        break;
      }
    }
  }
  uint8_t cursor = min(trendCursor, uint8_t(trendCount - 1));
  int16_t cursorX = graphX + int32_t(cursor) * graphW / (trendCount - 1);
  spr.drawFastVLine(cursorX, graphY, graphH, rgb565(255, 196, 64));
  uint8_t cursorIndex = (oldest + cursor) % TREND_POINTS;
  drawText(spr, x + w - 34, y + 3, percentText(values[cursorIndex]), rgb565(255, 196, 64));
}

String trendRateText() {
  if (trendCount < 2 || trendRatePerHour <= 0.05f) return "0%/h";
  return String(trendRatePerHour, 1) + "%/h";
}

String trendEtaText() {
  if (trendCount < 2 || trendEtaHours < 0.0f) return "ETA --";
  float hours = trendEtaHours;
  if (hours >= 24.0f) return "ETA " + String(hours / 24.0f, 1) + "d";
  return "ETA " + String(hours, 1) + "h";
}

void drawTrendPage(GFXcanvas16 &spr) {
  spr.fillScreen(rgb565(8, 10, 16));
  uint16_t cyan = rgb565(55, 209, 176);
  uint16_t green = rgb565(104, 220, 90);
  uint16_t muted = rgb565(128, 141, 166);
  uint16_t panel = rgb565(18, 23, 34);
  spr.fillRect(0, 0, SCREEN_W, 11, panel);
  drawText(spr, 4, 2, String("5H ") + String(trendWindowHours) + "H", cyan);
  bool fresh = trendLastSuccessMs && millis() - trendLastSuccessMs < 150000UL;
  drawRightText(spr, SCREEN_W - 2, 2, fresh ? "LIVE" : "OLD",
                fresh ? green : rgb565(255, 196, 64));
  drawTrendGraph(spr, 4, 16, 152, 96, "5H", trendFiveHour, cyan);
  drawText(spr, 7, 116, trendRateText(), muted);
  drawText(spr, 62, 116, trendEtaText(), muted);
  if (trendLatestResetSeconds >= 0) {
    drawRightText(spr, 158, 116, "R" + shortDuration(trendLatestResetSeconds), muted);
  }
}
#endif

String compactRate(float mbps) {
  if (mbps >= 1000.0f) {
    return String(mbps / 1000.0f, 1) + "G";
  }
  if (mbps >= 1.0f) {
    return String(mbps, 1) + "M";
  }
  return String(int(roundf(mbps * 1000.0f))) + "K";
}

String compactUptime(uint32_t seconds) {
  uint32_t days = seconds / 86400UL;
  uint32_t hours = (seconds % 86400UL) / 3600UL;
  if (days > 0) {
    return String(days) + "d" + String(hours) + "h";
  }
  return String(hours) + "h" + String((seconds % 3600UL) / 60UL) + "m";
}

void drawMetric(GFXcanvas16 &spr, int16_t x, int16_t y, const String &label, float percent,
                uint16_t color) {
  uint16_t muted = rgb565(128, 141, 166);
  drawText(spr, x, y, label, muted);
#ifdef XIAOMIAO
  constexpr int16_t metricWidth = 72;
  String value = percentText(percent);
  drawText(spr, x + metricWidth - value.length() * 6, y, value, color);
  drawProgressBar(spr, x, y + 12, metricWidth, 9, percent / 100.0f, color);
#else
  drawText(spr, x + 42, y, percentText(percent), color);
  drawProgressBar(spr, x, y + 10, 69, 7, percent / 100.0f, color);
#endif
}

void drawPerformancePage(GFXcanvas16 &spr) {
  spr.fillScreen(color565(config.performanceBackground));

  if (!config.wifiSsid.length()) {
    drawStatusPage(spr, "SETUP", "Open 192.168.4.1", "PASS " + apPassword);
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    drawStatusPage(spr, "WIFI...", config.wifiSsid, WiFi.softAPIP().toString());
    return;
  }
  if (!performance.ok) {
    drawStatusPage(spr, "NO PERF", performance.error.length() ? performance.error : "fetching",
                   WiFi.localIP().toString());
    return;
  }

  performance.displayCpu += (performance.cpuPercent - performance.displayCpu) * 0.16f;
  float selectedMemory = config.memoryMetric == 1 ? performance.memoryPressurePercent
                                                   : performance.memoryPercent;
  performance.displayMemory += (selectedMemory - performance.displayMemory) * 0.16f;
  performance.displayDisk += (performance.diskPercent - performance.displayDisk) * 0.16f;
  performance.displayLoad += (performance.loadPercent - performance.displayLoad) * 0.16f;

  uint16_t green = rgb565(119, 220, 87);
  uint16_t amber = rgb565(255, 196, 64);
  uint16_t muted = rgb565(128, 141, 166);
  uint16_t panel = rgb565(18, 23, 34);
  uint16_t cpuColor = color565(config.performanceCpuColor);
  uint16_t memoryColor = color565(config.performanceMemoryColor);
  uint16_t diskColor = color565(config.performanceDiskColor);
  uint16_t loadColor = color565(config.performanceLoadColor);
  uint16_t rxColor = color565(config.performanceRxColor);
  uint16_t txColor = color565(config.performanceTxColor);
#ifdef XIAOMIAO
  constexpr int16_t contentWidth = SCREEN_W;
  constexpr int16_t rightColumnX = 84;
#else
  constexpr int16_t contentWidth = SCREEN_W - 7;
  constexpr int16_t rightColumnX = 80;
#endif

  spr.fillRect(0, 0, contentWidth, 11, panel);
  drawText(spr, 4, 2, "PERF", cpuColor);
  drawText(spr, 40, 2, performance.hostName.substring(0, (contentWidth - 74) / 6), muted);
  drawRightText(spr, contentWidth - 2, 2, WiFi.RSSI() > -67 ? "WiFi" : "wifi",
                WiFi.RSSI() > -67 ? green : amber);

#ifdef XIAOMIAO
  drawMetric(spr, 4, 18, "CPU", performance.displayCpu, cpuColor);
  drawMetric(spr, rightColumnX, 18, config.memoryMetric == 1 ? "PRES" : "MEM",
             performance.displayMemory, memoryColor);
  drawMetric(spr, 4, 48, "DSK", performance.displayDisk, diskColor);
  drawMetric(spr, rightColumnX, 48, "LOAD", performance.displayLoad, loadColor);
  spr.fillRect(0, 78, contentWidth, 33, panel);
  drawText(spr, 4, 81, "RX " + compactRate(performance.netRxMbps), rxColor);
  drawText(spr, rightColumnX, 81, "TX " + compactRate(performance.netTxMbps), txColor);
  drawText(spr, 4, 98, "UP " + compactUptime(performance.uptimeSeconds), muted);
  drawCodexStatusIndicator(spr, SCREEN_W - 4, 0, SCREEN_H, true, true);
  return;
#endif

  drawMetric(spr, 4, 15, "CPU", performance.displayCpu, cpuColor);
  drawMetric(spr, rightColumnX, 15, config.memoryMetric == 1 ? "PRES" : "MEM",
             performance.displayMemory, memoryColor);
  drawMetric(spr, 4, 38, "DSK", performance.displayDisk, diskColor);
  drawMetric(spr, rightColumnX, 38, "LOAD", performance.displayLoad, loadColor);

  spr.fillRect(0, 59, contentWidth, 21, panel);
  drawText(spr, 4, 62, "RX " + compactRate(performance.netRxMbps), rxColor);
  String txText = "TX " + compactRate(performance.netTxMbps);
  drawText(spr, contentWidth - 4 - txText.length() * 6, 62, txText, txColor);
  drawText(spr, 4, 71, "UP " + compactUptime(performance.uptimeSeconds), muted);
  drawCodexStatusIndicator(spr, SCREEN_W - 4, 0, SCREEN_H, true, true);
}

void setup() {
#ifdef XIAOMIAO
  Serial.begin(115200);
  Serial.println("[Codex] XiaoMiao monitor starting");
  pinMode(22, OUTPUT);
  digitalWrite(22, HIGH); // Deselect the shared SPI SD reader, even without a card.
  pinMode(34, INPUT);
  pinMode(12, INPUT_PULLUP);
  // Launcher wiring: Up=2, Down=13, Left=27, Right=35.
  pinMode(2, INPUT_PULLUP);
  pinMode(13, INPUT_PULLUP);
  pinMode(27, INPUT_PULLUP);
  pinMode(35, INPUT);
#else
  pinMode(PIN_BLK, OUTPUT);
  backlightPwmReady = ledcAttach(PIN_BLK, BACKLIGHT_PWM_HZ, BACKLIGHT_PWM_BITS);
#endif
  writeBacklight(BACKLIGHT_MAX_DUTY);
  loadConfig();

  SPI.begin(PIN_SCL, -1, PIN_SDA, PIN_CS);
#ifdef XIAOMIAO
  // Adafruit aliases REDTAB and 144GREENTAB to 0x01, selecting a square panel.
  // GREENTAB selects native 128x160; the XiaoMiao panel uses zero offsets.
  tft.initR(INITR_GREENTAB);
  tft.setPanelOffset(0, 0);
#else
  tft.initR(INITR_MINI160x80);
#endif
  tft.setSPISpeed(10000000);
#ifndef XIAOMIAO
  tft.setPanelOffset(DISPLAY_COL_START, DISPLAY_ROW_START);
#endif
  applyScreenDirection();
  tft.invertDisplay(
#ifdef XIAOMIAO
    false
#else
    DISPLAY_INVERTED
#endif
  );
  tft.fillScreen(ST77XX_BLACK);
#ifdef XIAOMIAO
  Serial.printf("[Display] %dx%d rotation=%u ST7735 zero-offset\n", tft.width(), tft.height(), tft.getRotation());
#endif
#ifdef XM_STANDALONE_OTA
  Serial.println(XM_OTA_MARKER);
#endif
  writeBacklight(BACKLIGHT_MAX_DUTY);

  frameA.setTextSize(1);
  frameB.setTextSize(1);
  frameA.setTextWrap(false);
  frameB.setTextWrap(false);

#ifndef XIAOMIAO
  LittleFS.begin(true);
#endif
  setupNetwork();
}

void loop() {
#ifdef XIAOMIAO
  static bool previousA = false, previousB = false;
  static bool previousUp = false, previousDown = false;
  static bool previousLeft = false, previousRight = false;
  static uint32_t lastButton = 0, comboSince = 0;
  bool a = digitalRead(34) == LOW, b = digitalRead(12) == LOW;
  bool up = digitalRead(2) == LOW, down = digitalRead(13) == LOW;
  bool left = digitalRead(27) == LOW, right = digitalRead(35) == LOW;
  if (a && b) {
    if (!comboSince) comboSince = millis();
    if (millis() - comboSince > 1500) {
#ifdef XM_STANDALONE_OTA
      showOtaInfo = true;
#else
      const esp_partition_t *launcher = esp_partition_find_first(
        ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_TEST, nullptr);
      if (launcher && esp_ota_set_boot_partition(launcher) == ESP_OK) ESP.restart();
#endif
    }
  } else {
    comboSince = 0;
    if (millis() - lastButton > 200) {
      if (a && !previousA) {
#ifdef XM_STANDALONE_OTA
        showOtaInfo = false;
#endif
        config.displayMode = (config.displayMode + 1) % 3;
        lastTrendFetchMs = 0;
        footerUntilMs = millis() + 2500;
        usagePageDirty = true;
        sendDeviceStatus("status");
        lastButton = millis();
      } else if (config.displayMode == 2 &&
                 (up && !previousUp || down && !previousDown ||
                  left && !previousLeft || right && !previousRight)) {
        if (up && !previousUp) {
          trendWindowHours = trendWindowHours == 6 ? 24 : trendWindowHours == 12 ? 6 : 12;
          lastTrendFetchMs = 0;
        } else if (down && !previousDown) {
          trendWindowHours = trendWindowHours == 6 ? 12 : trendWindowHours == 12 ? 24 : 6;
          lastTrendFetchMs = 0;
        } else if (left && !previousLeft) {
          if (trendCount > 0 && trendCursor > 0) trendCursor--;
        } else if (right && !previousRight) {
          if (trendCount > 0 && trendCursor + 1 < trendCount) trendCursor++;
        }
        footerUntilMs = millis() + 2500;
        usagePageDirty = true;
        lastButton = millis();
      } else if ((down && !previousDown) || (right && !previousRight)) {
        config.displayMode = (config.displayMode + 1) % 3;
        lastTrendFetchMs = 0;
        footerUntilMs = millis() + 2500;
        usagePageDirty = true;
        sendDeviceStatus("status");
        lastButton = millis();
      } else if ((up && !previousUp) || (left && !previousLeft)) {
        config.displayMode = (config.displayMode + 2) % 3;
        lastTrendFetchMs = 0;
        footerUntilMs = millis() + 2500;
        usagePageDirty = true;
        sendDeviceStatus("status");
        lastButton = millis();
      }
      if (b && !previousB) {
#ifdef XM_STANDALONE_OTA
        showOtaInfo = false;
#endif
        config.displayMode = (config.displayMode + 2) % 3;
        lastTrendFetchMs = 0;
        footerUntilMs = millis() + 2500;
        usagePageDirty = true;
        sendDeviceStatus("status");
        lastButton = millis();
      }
    }
  }
  previousA = a;
  previousB = b;
  previousUp = up;
  previousDown = down;
  previousLeft = left;
  previousRight = right;
#endif
  webServer.handleClient();
#ifdef XM_STANDALONE_OTA
  if (xmOtaReboot && millis() - xmOtaRebootAt > 1000) ESP.restart();
#endif
  connectWifiIfNeeded();
  setupMdnsIfNeeded();
  setupWebSocketIfNeeded();
  if (wsStarted) {
    webSocket.loop();
  }
#ifdef XIAOMIAO
  fetchNasTrendIfNeeded();
#endif
  updateWsFailSafe();
  if (wsConnected &&
      (lastDeviceStatusMs == 0 || millis() - lastDeviceStatusMs >= DEVICE_STATUS_MS)) {
    lastDeviceStatusMs = millis();
    sendDeviceStatus("status");
  }
  updateBacklight();

  uint32_t now = millis();
  constexpr uint32_t renderInterval = 50;
  if (lastRenderMs != 0 && now - lastRenderMs < renderInterval) {
    delay(2);
    return;
  }
  lastRenderMs = now;

  if (config.displayMode == 0) {
    const bool usagePageReady =
        config.wifiSsid.length() && WiFi.status() == WL_CONNECTED && usage.ok;
    if (usagePageDirty || usagePageReady != usagePageReadyCached || !usagePageReady) {
      drawUsagePage(frameA);
      usagePageDirty = false;
      usagePageReadyCached = usagePageReady;
    }
    memcpy(frameB.getBuffer(), frameA.getBuffer(),
           SCREEN_W * SCREEN_H * sizeof(uint16_t));
    drawCodexStatusBar(frameB);
  } else if (config.displayMode == 1) {
    drawPerformancePage(frameB);
  } else {
#ifdef XIAOMIAO
    drawTrendPage(frameB);
#else
    drawPerformancePage(frameB);
#endif
  }
#ifdef XIAOMIAO
#ifdef XM_STANDALONE_OTA
  if (showOtaInfo) {
    frameB.fillScreen(ST77XX_BLACK);
    drawText(frameB, 4, 8, "OTA UPDATE", ST77XX_CYAN, 2);
    drawText(frameB, 4, 35, "http://" + WiFi.localIP().toString(), ST77XX_WHITE);
    drawText(frameB, 4, 49, "/ota   User: admin", ST77XX_WHITE);
    drawText(frameB, 4, 69, "Password:", ST77XX_CYAN);
    drawText(frameB, 4, 84, XM_OTA_PASSWORD, ST77XX_WHITE);
    drawText(frameB, 4, 103, "A: Back", ST77XX_WHITE);
  }
#endif
  if (config.displayMode != 2 && millis() < footerUntilMs) {
    frameB.fillRect(0, 116, SCREEN_W, 12, rgb565(18, 23, 34));
#ifdef XM_STANDALONE_OTA
    drawText(frameB, 3, 119, "A:Page B:Link AB:OTA", ST77XX_WHITE);
#else
    drawText(frameB, 3, 119, "A:Page  B:Link  AB:Exit", ST77XX_WHITE);
#endif
  }
  tft.drawRGBBitmap(0, 0, frameB.getBuffer(), SCREEN_W, SCREEN_H);
#else
  tft.drawRGBBitmap(0, 0, frameB.getBuffer(), SCREEN_W, SCREEN_H);
#endif
}
