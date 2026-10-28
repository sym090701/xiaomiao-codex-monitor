#pragma once
#include <esp_ota_ops.h>
#include "xiaomiao_ota_secret.h"

static const char XM_OTA_MARKER[] = "XIAOMIAO_CODEX_STANDALONE_OTA_V1";
static esp_ota_handle_t xmOtaHandle = 0;
static bool xmOtaActive = false, xmOtaOk = false, xmOtaReboot = false;
static String xmOtaError, xmOtaNonce;
static uint32_t xmOtaNonceAt = 0, xmOtaRebootAt = 0;
static size_t xmOtaBytes = 0, xmOtaMarkerMatch = 0;
static bool xmOtaMarkerFound = false;
static const esp_partition_t *xmOtaTarget = nullptr;

static void xmOtaFail(const char *message) {
  if (xmOtaActive) esp_ota_abort(xmOtaHandle);
  xmOtaActive = false;
  xmOtaOk = false;
  xmOtaError = message;
}

static bool xmOtaAuthenticated() {
  return webServer.authenticate("admin", XM_OTA_PASSWORD);
}

static void xmSetupOta() {
  const char *headers[] = {"Origin", "Host"};
  webServer.collectHeaders(headers, 2);
  webServer.on("/api/device-info", HTTP_GET, []() {
    String body = String("{\"screen_width\":") + tft.width() + ",\"screen_height\":" + tft.height() +
      ",\"rotation\":" + tft.getRotation() + ",\"standalone_ota\":true}";
    webServer.send(200, "application/json", body);
  });
  webServer.on("/api/ota", HTTP_GET, []() {
    if (!xmOtaAuthenticated()) { webServer.requestAuthentication(DIGEST_AUTH); return; }
    const esp_partition_t *running = esp_ota_get_running_partition();
    uint8_t hash[32];
    String digest;
    if (esp_partition_get_sha256(running, hash) == ESP_OK) {
      for (uint8_t b : hash) { char byte[3]; snprintf(byte, sizeof(byte), "%02x", b); digest += byte; }
    }
    String body = String("{\"running\":\"") + running->label + "\",\"address\":" + running->address +
      ",\"slot_size\":" + running->size + ",\"image_sha256\":\"" + digest + "\"}";
    webServer.send(200, "application/json", body);
  });
  webServer.on("/ota", HTTP_GET, []() {
    if (!xmOtaAuthenticated()) { webServer.requestAuthentication(DIGEST_AUTH); return; }
    xmOtaNonce = String(esp_random(), HEX) + String(esp_random(), HEX);
    xmOtaNonceAt = millis();
    webServer.sendHeader("Cache-Control", "no-store");
    webServer.send(200, "text/html", String("<!doctype html><meta name='viewport' content='width=device-width'><h2>XiaoMiao OTA</h2>") +
      "<p>Standalone app BIN only. Keep power connected.</p><form method='POST' action='/ota?nonce=" + xmOtaNonce +
      "' enctype='multipart/form-data'><input type='file' name='firmware' accept='.bin' required><button>Update</button></form>");
  });
  webServer.on("/ota", HTTP_POST, []() {
    if (!xmOtaAuthenticated()) { webServer.requestAuthentication(DIGEST_AUTH); return; }
    if (!xmOtaOk) { webServer.send(400, "text/plain", xmOtaError.length() ? xmOtaError : "No valid image"); return; }
    webServer.send(200, "text/plain", "Firmware verified. Rebooting.");
    xmOtaReboot = true;
    xmOtaRebootAt = millis();
  }, []() {
    HTTPUpload &upload = webServer.upload();
    if (upload.status == UPLOAD_FILE_START) {
      if (xmOtaActive) xmOtaFail("Previous upload aborted");
      xmOtaOk = false;
      xmOtaError = "Unauthorized upload";
      if (!xmOtaAuthenticated()) return;
      if (xmOtaNonce.isEmpty() || webServer.arg("nonce") != xmOtaNonce || millis() - xmOtaNonceAt > 120000) {
        xmOtaFail("Reload the OTA page before uploading"); return;
      }
      xmOtaNonce = "";
      String origin = webServer.header("Origin");
      if (origin.length() && origin != "http://" + webServer.header("Host")) {
        xmOtaFail("Origin rejected"); return;
      }
      const esp_partition_t *running = esp_ota_get_running_partition();
      xmOtaTarget = esp_ota_get_next_update_partition(nullptr);
      if (!running || !xmOtaTarget || running == xmOtaTarget ||
          (String(running->label) != "codex0" && String(running->label) != "codex1") ||
          (String(xmOtaTarget->label) != "codex0" && String(xmOtaTarget->label) != "codex1")) {
        xmOtaFail("Install the standalone partition layout over USB first"); return;
      }
      // Erase sectors as sequential writes arrive; erasing the entire slot here
      // can starve the idle task and trigger the watchdog on this ESP32.
      if (esp_ota_begin(xmOtaTarget, OTA_WITH_SEQUENTIAL_WRITES, &xmOtaHandle) != ESP_OK) {
        xmOtaFail("Could not prepare inactive slot"); return;
      }
      xmOtaActive = true;
      xmOtaBytes = 0;
      xmOtaMarkerFound = false;
      xmOtaMarkerMatch = 0;
      xmOtaError = "";
    } else if (upload.status == UPLOAD_FILE_WRITE && xmOtaActive) {
      if ((!xmOtaBytes && (!upload.currentSize || upload.buf[0] != 0xe9)) ||
          xmOtaBytes + upload.currentSize > xmOtaTarget->size) {
        xmOtaFail("Invalid image or image too large"); return;
      }
      for (size_t i = 0; i < upload.currentSize; ++i) {
        char c = upload.buf[i];
        if (c == XM_OTA_MARKER[xmOtaMarkerMatch]) ++xmOtaMarkerMatch;
        else xmOtaMarkerMatch = c == XM_OTA_MARKER[0] ? 1 : 0;
        if (xmOtaMarkerMatch == sizeof(XM_OTA_MARKER) - 1) {
          xmOtaMarkerFound = true;
          xmOtaMarkerMatch = 0;
        }
      }
      if (esp_ota_write(xmOtaHandle, upload.buf, upload.currentSize) != ESP_OK) {
        xmOtaFail("Flash write failed"); return;
      }
      xmOtaBytes += upload.currentSize;
      delay(1); // Let Wi-Fi and idle tasks run during a sustained upload.
    } else if (upload.status == UPLOAD_FILE_END && xmOtaActive) {
      if (!xmOtaMarkerFound || xmOtaBytes < 256) { xmOtaFail("Wrong project image"); return; }
      esp_err_t result = esp_ota_end(xmOtaHandle);
      xmOtaActive = false;
      if (result != ESP_OK) { xmOtaFail("Firmware checksum validation failed"); return; }
      if (esp_ota_set_boot_partition(xmOtaTarget) != ESP_OK) { xmOtaFail("Could not select updated slot"); return; }
      xmOtaOk = true;
      Serial.printf("[OTA] verified %u bytes -> %s\n", unsigned(xmOtaBytes), xmOtaTarget->label);
    } else if (upload.status == UPLOAD_FILE_ABORTED) {
      xmOtaFail("Upload interrupted");
    }
  });
}
