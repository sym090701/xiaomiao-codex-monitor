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
    observed_dates = set()
    previous = None
    last_drop = None
    for epoch, remaining, kind in rows:
        local = dt.datetime.fromtimestamp(epoch, zone)
        if local.date() < today:
            observed_dates.add(local.date())
        if previous is not None:
            elapsed = epoch - previous[0]
            drop = previous[1] - remaining
            # Ignore gaps, reset jumps, and flat samples. A request directly
            # around a known preheat is not evidence of human activity.
            suppressed = kind == "codex_preheat" or any(
                abs(epoch - event) <= 600 for event in excluded if number(event))
            if 0 < elapsed <= 900 and drop > 0.05 and not suppressed:
                if (last_drop is None or epoch - last_drop >= 3600) and local.date() < today:
                    first_by_date.setdefault(local.date(), local.hour * 60 + local.minute)
                last_drop = epoch
        previous = (epoch, remaining)
    empty["observed_days"] = len(observed_dates)
    candidates = []
    for offset in range(2):
        date = today + dt.timedelta(days=offset)
        # Prefer matching weekdays; with less than 3 weeks, pool workdays or
        # weekend days. Never silently fall back to an arbitrary clock time.
        exact = [minute for day, minute in first_by_date.items()
                 if day.weekday() == date.weekday()]
        grouped = [minute for day, minute in first_by_date.items()
                   if (day.weekday() < 5) == (date.weekday() < 5)]
        values, basis = (exact, "weekday") if len(exact) >= min_events else (grouped, "workday" if date.weekday() < 5 else "weekend")
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
            candidates.append({"predicted_at_epoch": predicted,
                               "support_days": len(cluster),
                               "observed_days": len(observed_dates), "basis": basis})
    return min(candidates, key=lambda item: item["predicted_at_epoch"]) if candidates else empty
