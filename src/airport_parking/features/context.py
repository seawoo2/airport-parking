"""Flight/calendar features using only snapshots available at forecast cutoff."""
from datetime import timedelta
import json
from pathlib import Path

import numpy as np
import pandas as pd

from airport_parking.features.congestion import TIMEZONE

NUMERIC = ["is_holiday", "holiday_eve", "day_after_holiday", "is_day_off", "holiday_available",
           "flight_arrival_available", "flight_departure_available",
           "flight_arrival", "flight_departure", "flight_cancelled_arrival", "flight_cancelled_departure",
           "flight_arrival_previous_2h", "flight_departure_next_2h",
           "flight_arrival_cumulative", "flight_departure_cumulative",
           "flight_arrival_daily", "flight_departure_daily"]
TERMINALS = {"P01": "T1", "P02": "T1", "P03": "T2"}


def load_context(snapshot):
    path = Path(snapshot) / "context_snapshots.csv"
    if not path.exists():
        return pd.DataFrame(columns=["id", "kind", "fetched_at", "scope", "records"])
    data = pd.read_csv(path)
    if not {"id", "kind", "fetched_at", "scope", "records"}.issubset(data.columns):
        raise ValueError("Missing context snapshot columns")
    data["fetched_at"] = pd.to_datetime(data.fetched_at, format="mixed", utc=True, errors="raise")
    if data.id.duplicated().any() or data.fetched_at.isna().any():
        raise ValueError("Invalid context identity or timestamp")
    for field, expected in (("scope", dict), ("records", list)):
        data[field] = data[field].map(json.loads)
        if not data[field].map(lambda value: isinstance(value, expected)).all():
            raise ValueError("Invalid context JSON")
    return data


def context_for_day(context, target_day, cutoff):
    known = context.loc[context.fetched_at <= cutoff].sort_values(["fetched_at", "id"]) if context is not None else pd.DataFrame()
    hours = pd.date_range(pd.Timestamp(target_day, tz=TIMEZONE), periods=24, freq="h").tz_convert("UTC")
    calendars = {}
    flights = {}
    if not known.empty:
        for batch in known.itertuples():
            if batch.kind == "holidays":
                calendars[(int(batch.scope["year"]), int(batch.scope["month"]))] = batch
            elif (batch.kind == "flights" and batch.scope.get("target_date") == str(target_day)
                  and batch.scope.get("time_basis") == "scheduled"
                  and batch.fetched_at >= cutoff - pd.Timedelta(hours=12)):
                flights[batch.scope["direction"]] = batch

    def holiday(day):
        batch = calendars.get((day.year, day.month))
        if batch is None:
            return np.nan
        return int(any(str(item.get("locdate")) == day.strftime("%Y%m%d") and item.get("isHoliday") == "Y"
                       for item in batch.records))

    is_holiday = holiday(target_day)
    calendar_batch = calendars.get((target_day.year, target_day.month))
    calendar_features = {"is_holiday": is_holiday, "holiday_eve": holiday(target_day + timedelta(days=1)),
                         "day_after_holiday": holiday(target_day - timedelta(days=1)),
                         "is_day_off": 1 if target_day.weekday() >= 5 else is_holiday,
                         "holiday_available": int(calendar_batch is not None),
                         "holiday_batch_id": calendar_batch.id if calendar_batch else np.nan,
                         "holiday_fetched_at": calendar_batch.fetched_at if calendar_batch else pd.NaT}
    result = {}
    for terminal in ("T1", "T2"):
        table = pd.DataFrame(index=hours)
        for direction in ("arrival", "departure"):
            table[f"flight_{direction}"] = np.nan
            table[f"flight_cancelled_{direction}"] = np.nan
        result[terminal] = table
    for direction, batch in flights.items():
        if direction not in ("arrival", "departure"):
            raise ValueError("Invalid context flight direction")
        # Choose the physical flight first. A shared flight number may recur on
        # the same day, so scheduled datetime is part of its grouping key.
        groups = {}
        ordered = sorted(batch.records, key=lambda item: str(item.get("codeshare", "")).lower() == "slave")
        for item in ordered:
            scheduled = str(item.get("scheduleDateTime", ""))
            if str(item.get("codeshare", "")).lower() == "slave" and not item.get("masterflightid"):
                raise ValueError("Codeshare flight is missing its master flight number")
            canonical = str(item.get("masterflightid") or item.get("flightId") or "")
            if not canonical or len(scheduled) not in (12, 14):
                raise ValueError("Invalid flight feature record")
            key = (canonical, scheduled)
            groups.setdefault(key, item)
        for table in result.values():
            table[f"flight_{direction}"] = 0.0
            table[f"flight_cancelled_{direction}"] = 0.0
        for item in groups.values():
            terminal = TERMINALS.get(item.get("terminalid"))
            if terminal is None:
                raise ValueError("Unknown flight terminal: " + str(item.get("terminalid")))
            text = str(item["scheduleDateTime"])
            hour = pd.Timestamp(pd.to_datetime(text, format="%Y%m%d%H%M" if len(text) == 12 else "%Y%m%d%H%M%S"), tz=TIMEZONE).floor("h").tz_convert("UTC")
            if hour not in hours:
                raise ValueError("Flight feature lies outside target date")
            cancelled = "결항" in str(item.get("remark", "")) or "cancel" in str(item.get("remark", "")).lower()
            field = f"flight_cancelled_{direction}" if cancelled else f"flight_{direction}"
            result[terminal].loc[hour, field] += 1
    for table in result.values():
        for name, value in calendar_features.items():
            table[name] = value
        for direction in ("arrival", "departure"):
            batch = flights.get(direction)
            table[f"flight_{direction}_available"] = int(batch is not None)
            table[f"flight_{direction}_batch_id"] = batch.id if batch else np.nan
            table[f"flight_{direction}_fetched_at"] = batch.fetched_at if batch else pd.NaT
            column = table[f"flight_{direction}"]
            table[f"flight_{direction}_daily"] = column.sum(min_count=1)
            table[f"flight_{direction}_cumulative"] = column.cumsum()
        table["flight_arrival_previous_2h"] = table.flight_arrival.shift(1).rolling(2, min_periods=1).sum()
        table["flight_departure_next_2h"] = table.flight_departure.shift(-1)[::-1].rolling(2, min_periods=1).sum()[::-1]
        if "arrival" in flights:
            table.loc[hours[0], "flight_arrival_previous_2h"] = 0
        if "departure" in flights:
            table.loc[hours[-1], "flight_departure_next_2h"] = 0
    return result


def coverage(frame):
    return {name: {"available_rows": int(frame[name].eq(1).sum()), "total_rows": len(frame)}
            for name in ("holiday_available", "flight_arrival_available", "flight_departure_available") if name in frame}
