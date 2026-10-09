"""Daily 17:15 snapshots for next-day hourly occupancy forecasting."""

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from airport_parking.features.congestion import TIMEZONE
from airport_parking.features.context import NUMERIC as CONTEXT_NUMERIC, context_for_day

CATEGORICAL = ["lot_name", "terminal", "lot_type"]
NUMERIC = [
    "current_ratio", "current_capacity", "anchor_age_minutes", "lead_hours",
    "hour_sin", "hour_cos", "weekday_sin", "weekday_cos", "is_weekend",
    "previous_day_same_hour", "previous_week_same_hour",
    "previous_day_max", "previous_day_min", "previous_week_max", "previous_week_min",
    "arrival", "departure", "arrival_previous_2h", "departure_next_2h",
    "arrival_cumulative", "departure_cumulative", "arrival_daily", "departure_daily",
]
BASE_NUMERIC = NUMERIC.copy()
NUMERIC = NUMERIC + CONTEXT_NUMERIC
FEATURES = CATEGORICAL + NUMERIC


def cutoff_for(day, cutoff_time="17:15"):
    hour, minute = map(int, cutoff_time.split(":"))
    return pd.Timestamp(datetime.combine(date.fromisoformat(str(day)), time(hour, minute)), tz=TIMEZONE).tz_convert("UTC")


def hourly_truth(parking, as_of=None, minimum_samples=4):
    """An hourly mean ratio, requiring four distinct ten-minute slots.

    Partial hours remain unlabeled. Never fill gaps or use records unavailable
    at the evaluation/training cutoff. Max is retained as a diagnostic only.
    """
    data = parking.loc[parking.total_spaces > 0].copy()
    if as_of is not None:
        data = data.loc[(data.collected_at <= as_of) & (data.observed_at <= as_of)]
    data["target_hour"] = data.observed_at.dt.floor("h")
    data["slot"] = data.observed_at.dt.floor("10min")
    data["ratio"] = data.occupied_spaces / data.total_spaces
    data = data.sort_values("collected_at").drop_duplicates(["lot_name", "observed_at"], keep="first")
    grouped = data.groupby(["lot_name", "target_hour"], as_index=False).agg(
        actual_ratio=("ratio", "mean"), actual_max_ratio=("ratio", "max"), actual_min_ratio=("ratio", "min"),
        observation_slots=("slot", "nunique"), label_available_at=("collected_at", "max"),
    )
    complete = grouped.observation_slots >= minimum_samples
    if as_of is not None:
        complete &= grouped.target_hour + pd.Timedelta(hours=1) <= as_of
    grouped.loc[~complete, ["actual_ratio", "actual_max_ratio", "actual_min_ratio"]] = np.nan
    return grouped


def forecast_batch(passengers, target_day, cutoff, required_after=None):
    """Choose one complete D+1 batch known at cutoff, rather than revisions later."""
    data = passengers.loc[(passengers.fetched_at <= cutoff)
                          & (passengers.target_hour.dt.tz_convert(TIMEZONE).dt.date == target_day)].copy()
    if "day_offset" in data:
        data = data.loc[pd.to_numeric(data.day_offset) == 1]
    if required_after is not None:
        data = data.loc[data.fetched_at >= required_after]
    required_hours = pd.date_range(pd.Timestamp(target_day, tz=TIMEZONE), periods=24, freq="h").tz_convert("UTC")
    expected = {(hour, terminal, direction) for hour in required_hours
                for terminal in ("T1", "T2") for direction in ("arrival", "departure")}
    for _, batch in data.sort_values("fetched_at", ascending=False).groupby("batch_id", sort=False):
        keys = set(zip(batch.target_hour, batch.terminal, batch.direction))
        if len(batch) == 96 and keys == expected and batch.fetched_at.nunique() == 1:
            return batch
    raise ValueError(f"No complete D+1 passenger batch available by {cutoff.isoformat()} for {target_day}")


def build_day(parking, passengers, issue_day, cutoff_time="17:15", required_after=None, context=None):
    cutoff = cutoff_for(issue_day, cutoff_time)
    target_day = date.fromisoformat(str(issue_day)) + timedelta(days=1)
    batch = forecast_batch(passengers, target_day, cutoff, required_after)
    context_features = context_for_day(context, target_day, cutoff)
    known = parking.loc[(parking.collected_at <= cutoff) & (parking.observed_at <= cutoff)]
    anchors = known.sort_values("collected_at").groupby("lot_name").tail(1)
    anchors = anchors.loc[anchors.total_spaces > 0]
    if anchors.empty:
        raise ValueError("No parking observations available at prediction cutoff")
    past_truth = hourly_truth(known, cutoff).set_index(["lot_name", "target_hour"])
    rows = []
    for anchor in anchors.itertuples():
        terminal = anchor.lot_name[:2]
        if terminal not in ("T1", "T2"):
            raise ValueError("Parking terminal mapping is missing: " + anchor.lot_name)
        lot_type = "short" if "단기" in anchor.lot_name else "long" if "장기" in anchor.lot_name else "reservation" if "예약" in anchor.lot_name else "unknown"
        series = batch.loc[batch.terminal == terminal].pivot(index="target_hour", columns="direction", values="expected_passengers").sort_index()
        for target, forecast in series.iterrows():
            local = target.tz_convert(TIMEZONE)
            row = {
                "issue_date": str(issue_day), "target_date": str(target_day), "origin_at": cutoff,
                "target_hour": target, "lot_name": anchor.lot_name, "terminal": terminal, "lot_type": lot_type,
                "current_ratio": anchor.occupied_spaces / anchor.total_spaces, "current_capacity": anchor.total_spaces,
                "anchor_observed_at": anchor.observed_at, "anchor_collected_at": anchor.collected_at,
                "anchor_age_minutes": (cutoff - anchor.observed_at).total_seconds() / 60,
                "lead_hours": (target - cutoff).total_seconds() / 3600,
                "hour_sin": np.sin(local.hour * 2 * np.pi / 24), "hour_cos": np.cos(local.hour * 2 * np.pi / 24),
                "weekday_sin": np.sin(local.dayofweek * 2 * np.pi / 7), "weekday_cos": np.cos(local.dayofweek * 2 * np.pi / 7),
                "is_weekend": int(local.dayofweek >= 5), "forecast_batch_id": int(batch.batch_id.iloc[0]),
                "forecast_fetched_at": batch.fetched_at.iloc[0], "arrival": forecast.arrival, "departure": forecast.departure,
            }
            for delta, field in ((1, "previous_day_same_hour"), (7, "previous_week_same_hour")):
                key = (anchor.lot_name, target - pd.Timedelta(days=delta))
                row[field] = past_truth.loc[key, "actual_ratio"] if key in past_truth.index else np.nan
                prefix = "previous_day" if delta == 1 else "previous_week"
                for statistic in ("max", "min"):
                    row[f"{prefix}_{statistic}"] = past_truth.loc[key, f"actual_{statistic}_ratio"] if key in past_truth.index else np.nan
            before = series.loc[(series.index >= target - pd.Timedelta(hours=2)) & (series.index < target)]
            after = series.loc[(series.index > target) & (series.index <= target + pd.Timedelta(hours=2))]
            row.update(arrival_previous_2h=before.arrival.sum(), departure_next_2h=after.departure.sum(),
                       arrival_cumulative=series.loc[:target].arrival.sum(), departure_cumulative=series.loc[:target].departure.sum(),
                       arrival_daily=series.arrival.sum(), departure_daily=series.departure.sum())
            row.update(context_features[terminal].loc[target].to_dict())
            rows.append(row)
    return pd.DataFrame(rows)


def build_training(parking, passengers, as_of, cutoff_time="17:15", minimum_samples=4, context=None):
    """Train only from forecasts and hourly truths available at deployment time."""
    days = sorted(set(passengers.target_hour.dt.tz_convert(TIMEZONE).dt.date))
    truth = hourly_truth(parking, as_of, minimum_samples)
    parts, skipped = [], []
    for target_day in days:
        issue_day = target_day - timedelta(days=1)
        if cutoff_for(issue_day, cutoff_time) >= as_of:
            continue
        try:
            rows = build_day(parking, passengers, issue_day, cutoff_time,
                             required_after=cutoff_for(issue_day, cutoff_time) - pd.Timedelta(minutes=5), context=context)
        except ValueError as exc:
            skipped.append({"target_date": str(target_day), "reason": str(exc)})
            continue
        rows = rows.merge(truth, on=["lot_name", "target_hour"], how="left", validate="one_to_one")
        # Historical anchors must also be recent enough for the production policy.
        parts.append(rows.loc[(rows.anchor_age_minutes <= 20) & rows.actual_ratio.notna()])
    return (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()), skipped
