"""Predict recurring evening sessions from archived 5-hour quota snapshots.

Daytime sessions are observations, not independent automatic triggers. Session
rates never cross a detected quota reset or a long sampling/usage gap.
"""
import datetime as dt
import json
import math
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def extract_sessions(rows, excluded=()):
    """Group real decreases by session and stable 5h reset, across midnight."""
    excluded = [x for x in excluded if number(x)]
    excluded += [r[0] for r in rows if r[2] == "codex_preheat"]
    sessions, current = [], None
    previous = None
    for epoch, remaining, kind, reset in sorted(rows, key=lambda row: row[0]):
        suppressed = kind == "codex_preheat" or any(abs(epoch-x) <= 600 for x in excluded)
        if previous is None:
            previous = (epoch, remaining, reset)
            continue
        elapsed, drop = epoch - previous[0], previous[1] - remaining
        boundary = (number(reset) and number(previous[2]) and
                    abs(reset - previous[2]) > 180)
        previous = (epoch, remaining, reset)
        if current and (boundary or elapsed > 900 or drop < -0.5 or epoch - current["start"] > 18000):
            sessions.append(current)
            current = None
        if boundary or suppressed or not 0 < elapsed <= 900 or drop <= 0.05:
            continue
        reset_change = (current and number(reset) and number(current["reset"]) and
                        abs(reset - current["reset"]) > 180)
        if current and (epoch - current["end"] >= 3600 or reset_change):
            sessions.append(current)
            current = None
        if current is None:
            current = {"start": epoch, "end": epoch, "drop": 0.0,
                       "active_seconds": 0.0, "reset": reset}
        current["end"] = epoch
        current["drop"] += drop
        current["active_seconds"] += elapsed
        if number(reset):
            current["reset"] = reset
    if current:
        sessions.append(current)
    return sessions


def predict_usage(root, now, history_days=28, min_events=3, excluded=()):
    local_now = dt.datetime.fromtimestamp(now, ZONE)
    today = local_now.date()
    cutoff_today = dt.datetime.combine(today, dt.time(), ZONE).timestamp()
    empty = {"predicted_at_epoch": None, "support_days": 0,
             "observed_days": 0, "basis": "insufficient_history"}
    if root is None:
        return empty
    rows, observed = [], set()
    for offset in range(history_days, -1, -1):
        date = today - dt.timedelta(days=offset)
        path = Path(root) / (date.isoformat() + ".ndjson")
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        epoch = row.get("observed_at_epoch")
                        window = (row.get("usage") or {}).get("five_hour") or {}
                        remaining = window.get("remaining_percent")
                        reset = window.get("reset_at_epoch")
                        if number(epoch) and number(remaining) and epoch < cutoff_today:
                            rows.append((epoch, remaining, row.get("record_type"),
                                         reset if number(reset) else None))
                            observed.add(dt.datetime.fromtimestamp(epoch, ZONE).date())
                    except (ValueError, TypeError, AttributeError):
                        continue
        except FileNotFoundError:
            continue
    empty["observed_days"] = len(observed)
    sessions = extract_sessions(rows, excluded)
    daytime_count = sum(1 for session in sessions if
                        6 <= dt.datetime.fromtimestamp(session["start"], ZONE).hour < 17)
    empty["daytime_sessions"] = daytime_count
    # Only evening sessions with a measurable amount qualify for automatic
    # preheat; small daytime bursts remain useful observations, not triggers.
    eligible = []
    for session in sessions:
        local = dt.datetime.fromtimestamp(session["start"], ZONE)
        if 17 <= local.hour < 24 and session["drop"] >= 1:
            eligible.append((local, session))
    for offset in range(2):
        date = today + dt.timedelta(days=offset)
        same_day = [(d, s) for d, s in eligible if d.weekday() == date.weekday()]
        grouped = [(d, s) for d, s in eligible if (d.weekday() < 5) == (date.weekday() < 5)]
        selected, basis = (same_day, "weekday") if len({d.date() for d, _ in same_day}) >= min_events else (
            grouped, "workday" if date.weekday() < 5 else "weekend")
        # At most one vote per day within each candidate cluster.
        minutes = [(d.hour * 60 + d.minute, d.date(), s) for d, s in selected]
        clusters = []
        for start, _, _ in minutes:
            by_date = {}
            for minute, day, session in minutes:
                if start <= minute <= start + 90:
                    old = by_date.get(day)
                    if old is None or session["drop"] > old[1]["drop"]:
                        by_date[day] = (minute, session)
            clusters.append(list(by_date.values()))
        if not clusters:
            continue
        cluster = max(clusters, key=lambda c: (len(c), sum(s["drop"] for _, s in c)))
        days = len({d.date() for d, _ in selected})
        if len(cluster) < min_events or len(cluster) / days < 0.6:
            continue
        minute = int(median([m for m, _ in cluster]))
        predicted = dt.datetime.combine(date, dt.time(minute // 60, minute % 60), ZONE).timestamp()
        if predicted <= now:
            continue
        durations = []
        for _, session in cluster:
            wall = session["end"] - session["start"]
            if session["drop"] >= 10 and session["active_seconds"] >= 600 and wall >= 600:
                estimate = wall * 80 / session["drop"]
                if 3600 <= estimate <= 18000:
                    durations.append(estimate)
        dynamic = len(durations) >= min_events
        duration = median(durations) if dynamic else None
        lead = int(round(min(14400, max(3600, 18000 - duration + 900)) / 300) * 300) if dynamic else 12600
        return {"predicted_at_epoch": predicted, "support_days": len(cluster),
                "observed_days": len(observed), "basis": basis,
                "session_kind": "evening", "daytime_sessions": daytime_count,
                "lead_seconds": lead,
                "duration_hours": round(duration / 3600, 2) if dynamic else None,
                "lead_basis": "historical_duration" if dynamic else "safe_default"}
    return empty
