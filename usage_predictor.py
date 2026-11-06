"""Conservative local-time prediction from archived quota consumption.

Only predicts the first observed usage session of a day. Quota percentages
cannot identify individual requests, so this is a habit estimate, not a promise.
"""
import datetime as dt
import json
import math
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def predict_usage(root, now, history_days=28, min_events=3, excluded=()):
    zone = ZoneInfo("Asia/Shanghai")
    today = dt.datetime.fromtimestamp(now, zone).date()
    empty = {"predicted_at_epoch": None, "support_days": 0,
             "observed_days": 0, "basis": "insufficient_history"}
    if root is None:
        return empty
    rows = []
    for offset in range(history_days, -1, -1):
        path = Path(root) / ((today - dt.timedelta(days=offset)).isoformat() + ".ndjson")
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        epoch = row.get("observed_at_epoch")
                        window = (row.get("usage") or {}).get("five_hour") or {}
                        remaining = window.get("remaining_percent")
                        if number(epoch) and number(remaining) and epoch <= now:
                            rows.append((epoch, remaining, row.get("record_type")))
                    except (ValueError, AttributeError, TypeError):
                        continue
        except FileNotFoundError:
            continue
    rows.sort(key=lambda row: row[0])
    excluded = list(excluded) + [epoch for epoch, _, kind in rows
                                if kind == "codex_preheat"]
    first_by_date = {}
    duration_by_date = {}
    observed_dates = set()
    rows_by_date = {}
    for epoch, remaining, kind in rows:
        local = dt.datetime.fromtimestamp(epoch, zone)
        if local.date() < today:
            observed_dates.add(local.date())
            rows_by_date.setdefault(local.date(), []).append((epoch, remaining, kind))
    for date, day_rows in rows_by_date.items():
        previous = None
        start_epoch = None
        start_remaining = None
        active_seconds = 0.0
        active_drop = 0.0
        for epoch, remaining, kind in day_rows:
            if kind == "codex_preheat" or any(
                    abs(epoch - event) <= 600 for event in excluded if number(event)):
                previous = (epoch, remaining)
                continue
            if previous is not None:
                elapsed = epoch - previous[0]
                drop = previous[1] - remaining
                if start_epoch is None and 0 < elapsed <= 900 and drop > 0.05:
                    start_epoch = epoch
                    start_remaining = previous[1]
                    first_by_date[date] = (epoch, int(dt.datetime.fromtimestamp(
                        epoch, zone).hour * 60 + dt.datetime.fromtimestamp(epoch, zone).minute))
                if start_epoch is not None and 0 < elapsed <= 900:
                    if drop > 0.05:
                        active_seconds += elapsed
                        active_drop += drop
                    # A 90 percentage-point fall is a complete enough session
                    # to estimate how long this user's window remains useful.
                    if start_remaining - remaining >= 90:
                        duration_by_date[date] = (epoch - start_epoch,
                                                  active_drop, active_seconds)
                        break
            previous = (epoch, remaining)
    empty["observed_days"] = len(observed_dates)
    candidates = []
    for offset in range(2):
        date = today + dt.timedelta(days=offset)
        # Prefer matching weekdays; with less than 3 weeks, pool workdays or
        # weekend days. Never silently fall back to an arbitrary clock time.
        exact_days = [day for day in first_by_date if day.weekday() == date.weekday()]
        grouped_days = [day for day in first_by_date
                        if (day.weekday() < 5) == (date.weekday() < 5)]
        selected_days, basis = (exact_days, "weekday") if len(exact_days) >= min_events else (
            grouped_days, "workday" if date.weekday() < 5 else "weekend")
        values = [first_by_date[day][1] for day in selected_days]
        if len(values) < min_events:
            continue
        # Use a recurring 90-minute cluster, not the average of unrelated
        # morning/evening usage. Each vote comes from a distinct date.
        cluster = max(([value for value in values if start <= value <= start + 90]
                       for start in values), key=lambda group: (len(group), -median(group)))
        if len(cluster) < min_events or len(cluster) / len(values) < 0.6:
            continue
        minute = int(median(cluster))
        predicted = dt.datetime.combine(date, dt.time(minute // 60, minute % 60), zone).timestamp()
        if predicted > now:
            duration_values = [duration_by_date[day][0] for day in selected_days
                               if day in duration_by_date
                               and abs(first_by_date[day][1] - minute) <= 90]
            dynamic = len(duration_values) >= min_events
            if dynamic:
                duration_seconds = median(duration_values)
                lead_seconds = min(14400, max(3600, 5 * 3600 - duration_seconds + 900))
                lead_seconds = int(round(lead_seconds / 300) * 300)
            else:
                duration_seconds = None
                lead_seconds = 12600
            candidates.append({"predicted_at_epoch": predicted,
                               "support_days": len(cluster),
                               "observed_days": len(observed_dates), "basis": basis})
            candidates[-1].update({"lead_seconds": lead_seconds,
                                   "duration_hours": (round(duration_seconds / 3600, 2)
                                                       if duration_seconds is not None else None),
                                   "lead_basis": "historical_duration" if dynamic else "safe_default"})
    return min(candidates, key=lambda item: item["predicted_at_epoch"]) if candidates else empty
