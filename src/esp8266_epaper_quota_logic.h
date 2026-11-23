#pragma once
#include <stdint.h>
#include <stddef.h>
#include <time.h>

struct EpaperQuota {
  bool valid = false;
  int fiveTenths = -1;
  int weeklyTenths = -1;
  uint32_t fiveResetEpoch = 0;
  uint32_t weeklyResetEpoch = 0;
  // Timestamp supplied by the quota server for the snapshot that was drawn.
  // It is display metadata and intentionally does not trigger a refresh.
  uint32_t updatedEpoch = 0;
};

inline bool epaperQuotaChanged(const EpaperQuota &a, const EpaperQuota &b) {
  // A full window has just reset when its remaining value is 100.0%. The
  // service may still return a moving reset timestamp while that window is
  // full; that timestamp alone should not cause an e-paper refresh.
  const bool fiveResetChanged =
      a.fiveResetEpoch != b.fiveResetEpoch &&
      !(a.fiveTenths == 1000 && b.fiveTenths == 1000);
  const bool weeklyResetChanged =
      a.weeklyResetEpoch != b.weeklyResetEpoch &&
      !(a.weeklyTenths == 1000 && b.weeklyTenths == 1000);
  return a.valid != b.valid || a.fiveTenths != b.fiveTenths ||
         a.weeklyTenths != b.weeklyTenths || fiveResetChanged ||
         weeklyResetChanged;
}

inline bool epaperRefreshDue(bool hasPainted, bool hasAttempted, uint32_t now,
                             uint32_t lastAttempt, uint32_t minimumInterval,
                             const EpaperQuota &latest, const EpaperQuota &painted) {
  if (hasPainted && !epaperQuotaChanged(latest, painted)) return false;
  return !hasAttempted || uint32_t(now - lastAttempt) >= minimumInterval;
}

inline bool epaperResetTime(uint32_t epoch, char *out, size_t size) {
  if (!epoch || size < 12) return false;
  // China Standard Time is UTC+08:00. An absolute timestamp avoids periodic redraws.
  time_t local = static_cast<time_t>(epoch) + 8 * 3600;
  struct tm tmValue;
  if (!gmtime_r(&local, &tmValue)) return false;
  int month = tmValue.tm_mon + 1, day = tmValue.tm_mday;
  int hour = tmValue.tm_hour, minute = tmValue.tm_min;
  if (month < 1 || month > 12 || day < 1 || day > 31 ||
      hour < 0 || hour > 23 || minute < 0 || minute > 59) return false;
  out[0] = char('0' + month / 10); out[1] = char('0' + month % 10); out[2] = '-';
  out[3] = char('0' + day / 10); out[4] = char('0' + day % 10); out[5] = ' ';
  out[6] = char('0' + hour / 10); out[7] = char('0' + hour % 10); out[8] = ':';
  out[9] = char('0' + minute / 10); out[10] = char('0' + minute % 10); out[11] = '\0';
  return true;
}
