"""Pure presentation and input helpers, intentionally independent of the backend."""

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NUTRIENTS = ("calories", "protein", "carbs", "fat", "fiber")
MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")
EVENT_TYPES = ("meal", "water", "weight", "supplement")


def timezone_for(name):
    try:
        return ZoneInfo(name or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Enter a valid IANA timezone, such as Asia/Kolkata or America/New_York.") from exc


def local_now(profile):
    return datetime.now(timezone_for(profile.get("timezone")))


def local_timestamp(day, clock, zone, *, fold=0):
    naive = datetime.combine(day, clock.replace(tzinfo=None))
    aware = naive.replace(tzinfo=timezone_for(zone), fold=fold)
    roundtrip = aware.astimezone(ZoneInfo("UTC")).astimezone(aware.tzinfo)
    if roundtrip.replace(tzinfo=None) != naive:
        raise ValueError("This local time does not exist because of daylight saving. Choose a different time.")
    return aware.isoformat()


def parse_timestamp(value, profile, fallback_day=None):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone_for(profile.get("timezone")))
    except (ValueError, TypeError):
        now = local_now(profile)
        return datetime.combine(fallback_day or now.date(), now.time(), tzinfo=now.tzinfo)


def number(value, label, *, positive=False, integer=False):
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise ValueError(f"{label} is required. Correct missing AI estimates before saving.")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{label} must be {'greater than zero' if positive else 'zero or greater'}.")
    if integer and not result.is_integer():
        raise ValueError(f"{label} must be a whole number.")
    return int(result) if integer else result


def clean_items(rows):
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Add at least one food item.")
    cleaned = []
    for index, row in enumerate(rows, 1):
        name = str(row.get("food_name") or "").strip()
        unit = str(row.get("unit") or "").strip()
        if not name or not unit:
            raise ValueError(f"Food {index}: name and portion unit are required.")
        item = {"food_name": name, "quantity": number(row.get("quantity"), f"{name}: quantity", positive=True), "unit": unit}
        item.update({key: number(row.get(key), f"{name}: {key}") for key in NUTRIENTS})
        cleaned.append(item)
    return cleaned


def clean_events(events):
    if not events:
        raise ValueError("Keep at least one event to confirm, or discard this draft.")
    cleaned = []
    for index, event in enumerate(events, 1):
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            raise ValueError(f"Event {index}: choose meal, water, weight, or supplement.")
        try:
            occurred = datetime.fromisoformat(str(event.get("occurred_at", "")).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Event {index}: an ISO date/time with timezone is required.") from exc
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            raise ValueError(f"Event {index}: the time must include a timezone offset.")
        item = {"type": kind, "occurred_at": occurred.isoformat()}
        if kind == "meal":
            if event.get("meal_type") not in MEAL_TYPES:
                raise ValueError(f"Event {index}: choose a meal type.")
            item.update(meal_type=event["meal_type"], items=clean_items(event.get("items")))
        elif kind == "water":
            item["amount_ml"] = number(event.get("amount_ml"), "Water (ml)", positive=True, integer=True)
        elif kind == "weight":
            item["weight_kg"] = number(event.get("weight_kg"), "Weight (kg)", positive=True)
        else:
            name = str(event.get("name") or "").strip()
            if not name:
                raise ValueError(f"Event {index}: supplement name is required.")
            if not isinstance(event.get("taken"), bool):
                raise ValueError(f"Event {index}: mark the supplement taken or not taken.")
            item.update(name=name, taken=event["taken"])
        cleaned.append(item)
    return cleaned


def history_chart_rows(days):
    rows = []
    for day in sorted(days, key=lambda item: item["date"]):
        records = day.get("weight_records") or []
        recorded = sorted(records, key=lambda item: item.get("recorded_at", ""))
        totals = day.get("totals") or {}
        expected = [item for item in day.get("supplements", []) if item.get("active", True)]
        taken = sum(item.get("status") == "taken" for item in expected)
        rows.append({
            "date": day["date"],
            "weight_kg": recorded[-1].get("weight_kg") if recorded else None,
            "calories": totals.get("calories", 0),
            "protein": totals.get("protein", 0),
            "water_ml": totals.get("water_ml", 0),
            "adherence": 100 * taken / len(expected) if expected else None,
        })
    return rows


def complete_history_days(days, start, end):
    """Keep untracked dates as zero-log days, not interpolated observations."""
    indexed = {str(item["date"]): item for item in days}
    current = date.fromisoformat(str(start))
    last = date.fromisoformat(str(end))
    result = []
    while current <= last:
        key = current.isoformat()
        result.append(indexed.get(key, {"date": key, "totals": {}, "weight_records": [], "supplements": []}))
        current += timedelta(days=1)
    return result
