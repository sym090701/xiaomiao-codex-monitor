#include "../src/esp8266_epaper_quota_logic.h"
#include <cassert>
#include <cstring>

int main() {
  char time[12] = {};
  assert(!epaperResetTime(0, time, sizeof(time)));
  assert(!epaperResetTime(1704067200U, time, 11));
  assert(epaperResetTime(1704067200U, time, sizeof(time)));
  assert(std::strcmp(time, "01-01 08:00") == 0);
  assert(epaperResetTime(1704038400U, time, sizeof(time)));
  assert(std::strcmp(time, "01-01 00:00") == 0);

  EpaperQuota before, next;
  before.valid = true;
  before.fiveTenths = 500;
  before.weeklyTenths = 900;
  before.fiveResetEpoch = 1704067200;
  before.weeklyResetEpoch = 1704672000;
  next = before;
  assert(!epaperQuotaChanged(before, next));
  assert(!epaperRefreshDue(true, true, 1000000, 0, 60000, next, before));
  next.fiveTenths++;
  assert(epaperQuotaChanged(before, next));
  assert(!epaperRefreshDue(true, true, 59999, 0, 60000, next, before));
  assert(epaperRefreshDue(true, true, 60000, 0, 60000, next, before));
  assert(epaperRefreshDue(false, false, 0, 0, 60000, next, before));
  next = before; next.fiveResetEpoch++;
  assert(epaperQuotaChanged(before, next));
  next = before; next.weeklyResetEpoch++;
  assert(epaperQuotaChanged(before, next));
  EpaperQuota full = before;
  full.fiveTenths = 1000;
  full.weeklyTenths = 1000;
  next = full; next.fiveResetEpoch++;
  assert(!epaperQuotaChanged(full, next));
  next = full; next.weeklyResetEpoch++;
  assert(!epaperQuotaChanged(full, next));
  next = before; next.fiveTenths = 999; next.fiveResetEpoch++;
  assert(epaperQuotaChanged(before, next));
  next = before; next.weeklyTenths = 999; next.weeklyResetEpoch++;
  assert(epaperQuotaChanged(before, next));
  next = before; next.valid = false;
  assert(epaperQuotaChanged(before, next));
  next = before; next.weeklyTenths = -1;
  assert(epaperQuotaChanged(before, next));
  // millis() wraps at 2^32; subtraction must remain well-defined.
  next = before; next.fiveTenths++;
  assert(epaperRefreshDue(true, true, 59500U, 0xfffffe0bU, 60000U, next, before));
  return 0;
}
