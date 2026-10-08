"""Build congestion features from information available at each prediction time."""

import json
from pathlib import Path

import numpy as np
import pandas as pd

TIMEZONE = "Asia/Seoul"
CATEGORICAL = ["lot_name", "terminal", "lot_type"]
NUMERIC = [
    "current_ratio", "occupied_spaces", "total_spaces", "observation_age_minutes",
    "lag_10_ratio", "lag_30_ratio", "lag_60_ratio", "change_30", "change_60",
    "hour_sin", "hour_cos", "weekday_sin", "weekday_cos", "is_weekend",
    "passengers_now_arrival", "passengers_now_departure",
    "passengers_target_arrival", "passengers_target_departure",
]
FEATURES = CATEGORICAL + NUMERIC


def timestamps(values):
    return pd.to_datetime(values, format="mixed", utc=True, errors="raise").astype("datetime64[ns, UTC]")


def load_snapshot(data_root: Path, snapshot: Path | None = None):
    root = data_root.resolve()
    if snapshot is None:
        pointer = json.loads((root / "latest.json").read_text(encoding="utf-8"))
        snapshot = (root / pointer["dataset_dir"]).resolve()
        if not snapshot.is_relative_to(root):
            raise ValueError("Latest snapshot must be inside the data directory")
    else:
        snapshot = snapshot.resolve()
    parking = pd.read_csv(snapshot / "parking.csv")
    passengers = pd.read_csv(snapshot / "passenger_forecasts.csv")
    required = {"lot_name", "observed_at", "collected_at", "occupied_spaces", "total_spaces"}
    if not required.issubset(parking.columns):
        raise ValueError("Missing required parking columns")
    if not {"terminal", "direction", "target_hour", "fetched_at", "expected_passengers"}.issubset(passengers.columns):
        raise ValueError("Missing required passenger columns")
    if parking.empty:
        raise ValueError("Parking export is empty")
    for column in ("observed_at", "collected_at"):
        parking[column] = timestamps(parking[column])
    for column in ("target_hour", "fetched_at"):
        passengers[column] = timestamps(passengers[column])
    for frame, columns in ((parking, ("occupied_spaces", "total_spaces")), (passengers, ("expected_passengers",))):
        for column in columns:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
            if not np.isfinite(frame[column]).all() or (frame[column] < 0).any() or (frame[column] % 1 != 0).any():
                raise ValueError("Counts must be nonnegative integers: " + column)
    if parking["lot_name"].isna().any() or parking["lot_name"].str.strip().eq("").any():
        raise ValueError("Missing lot name")
    if parking[["observed_at", "collected_at"]].isna().any().any():
        raise ValueError("Missing parking timestamp")
    if passengers[["target_hour", "fetched_at"]].isna().any().any():
        raise ValueError("Missing passenger timestamp")
    if not passengers["terminal"].isin(["T1", "T2"]).all() or not passengers["direction"].isin(["arrival", "departure"]).all():
        raise ValueError("Unknown passenger terminal or direction")
    duplicate_count = int(parking.duplicated(["lot_name", "observed_at"]).sum())
    parking = parking.sort_values("collected_at").drop_duplicates(["lot_name", "observed_at"], keep="first")
    valid_clock = parking["observed_at"] <= parking["collected_at"]
    clock_errors = int((~valid_clock).sum())
    parking = parking.loc[valid_clock].copy()
    if parking.empty:
        raise ValueError("No observations with valid timestamp ordering")
    parking = parking.sort_values(["lot_name", "collected_at", "observed_at"]).reset_index(drop=True)
    quality = describe_data(parking)
    quality.update(snapshot=str(snapshot), duplicate_rows_removed=duplicate_count, invalid_clock_rows_excluded=clock_errors)
    return parking, passengers, quality


def describe_data(parking):
    per_lot = []
    for name, group in parking.groupby("lot_name", sort=True):
        gaps = group.sort_values("observed_at")["observed_at"].diff().dt.total_seconds().div(60).dropna()
        per_lot.append({
            "lot_name": name, "rows": len(group),
            "first_observed_at": group.observed_at.min().isoformat(),
            "last_observed_at": group.observed_at.max().isoformat(),
            "gaps_over_20_minutes": int((gaps > 20).sum()),
            "max_gap_minutes": float(gaps.max()) if len(gaps) else None,
            "capacity_versions": int(group.total_spaces.nunique()),
            "inactive_rows": int(group.total_spaces.eq(0).sum()),
            "over_capacity_rows": int(((group.total_spaces > 0) & (group.occupied_spaces > group.total_spaces)).sum()),
        })
    return {
        "parking_rows": len(parking), "lots": int(parking.lot_name.nunique()),
        "first_observed_at": parking.observed_at.min().isoformat(),
        "last_observed_at": parking.observed_at.max().isoformat(),
        "span_days": (parking.observed_at.max() - parking.observed_at.min()).total_seconds() / 86400,
        "per_lot": per_lot,
    }


def build_features(parking, passengers, horizon_minutes=60):
    records = parking.loc[parking.total_spaces > 0].copy().reset_index(drop=True)
    records["origin_at"] = records.collected_at
    records["requested_target_at"] = records.origin_at + pd.Timedelta(minutes=horizon_minutes)
    records["current_ratio"] = records.occupied_spaces / records.total_spaces
    records["terminal"] = records.lot_name.str.extract(r"^(T[12])", expand=False).fillna("unknown")
    records["lot_type"] = records.lot_name.map(
        lambda name: "short" if "단기" in name else "long" if "장기" in name else "reservation" if "예약" in name else "unknown"
    )
    records["observation_age_minutes"] = (records.origin_at - records.observed_at).dt.total_seconds() / 60
    local_target = records.requested_target_at.dt.tz_convert(TIMEZONE)
    hour = local_target.dt.hour + local_target.dt.minute / 60
    weekday = local_target.dt.dayofweek
    records["hour_sin"] = np.sin(hour * 2 * np.pi / 24)
    records["hour_cos"] = np.cos(hour * 2 * np.pi / 24)
    records["weekday_sin"] = np.sin(weekday * 2 * np.pi / 7)
    records["weekday_cos"] = np.cos(weekday * 2 * np.pi / 7)
    records["is_weekend"] = (weekday >= 5).astype(int)
    for minutes in (10, 30, 60):
        records[f"lag_{minutes}_ratio"] = np.nan
    # Availability time, rather than row offset, prevents a long outage becoming a ten-minute lag.
    for name, group in records.groupby("lot_name", sort=False):
        history = parking.loc[parking.lot_name == name].sort_values("collected_at")
        known_times = pd.DatetimeIndex(history.collected_at)
        for index, row in group.iterrows():
            for minutes in (10, 30, 60):
                desired = row.origin_at - pd.Timedelta(minutes=minutes)
                position = known_times.searchsorted(desired, side="right") - 1
                if position >= 0:
                    previous = history.iloc[position]
                    if previous.total_spaces > 0 and desired - previous.observed_at <= pd.Timedelta(minutes=20):
                        records.at[index, f"lag_{minutes}_ratio"] = previous.occupied_spaces / previous.total_spaces
    for minutes in (30, 60):
        records[f"change_{minutes}"] = records.current_ratio - records[f"lag_{minutes}_ratio"]

    forecast_lookup = {}
    for key, group in passengers.groupby(["terminal", "direction", "target_hour"], sort=False):
        ordered = group.sort_values("fetched_at")
        if ordered.fetched_at.duplicated().any() and ordered.groupby("fetched_at").expected_passengers.nunique().gt(1).any():
            raise ValueError("Conflicting forecasts with the same availability timestamp")
        ordered = ordered.drop_duplicates("fetched_at", keep="first")
        forecast_lookup[key] = (pd.DatetimeIndex(ordered.fetched_at), ordered.expected_passengers.to_numpy())
    records["latest_forecast_used_at"] = pd.Series(pd.NaT, index=records.index, dtype="datetime64[ns, UTC]")
    for period in ("now", "target"):
        for direction in ("arrival", "departure"):
            column = f"passengers_{period}_{direction}"
            records[column] = np.nan
            for index, row in records.iterrows():
                target_hour = (row.origin_at if period == "now" else row.requested_target_at).floor("h")
                lookup = forecast_lookup.get((row.terminal, direction, target_hour))
                if lookup is None:
                    continue
                times, values = lookup
                position = times.searchsorted(row.origin_at, side="right") - 1
                if position >= 0:
                    records.at[index, column] = values[position]
                    previous = records.at[index, "latest_forecast_used_at"]
                    records.at[index, "latest_forecast_used_at"] = times[position] if pd.isna(previous) else max(previous, times[position])
    return records


def attach_targets(records, parking, tolerance_minutes=10):
    """Match actual observations near each requested target without filling outages."""
    parts = []
    truth = parking.loc[parking.total_spaces > 0].copy()
    truth["actual_ratio"] = truth.occupied_spaces / truth.total_spaces
    for name, group in records.groupby("lot_name", sort=False):
        future = truth.loc[truth.lot_name == name, ["observed_at", "collected_at", "actual_ratio", "total_spaces"]].rename(columns={
            "observed_at": "actual_observed_at", "collected_at": "label_available_at", "total_spaces": "actual_capacity",
        })
        merged = pd.merge_asof(group.sort_values("requested_target_at"), future.sort_values("actual_observed_at"),
                               left_on="requested_target_at", right_on="actual_observed_at", direction="nearest",
                               tolerance=pd.Timedelta(minutes=tolerance_minutes))
        invalid = (merged.actual_observed_at <= merged.origin_at) | (merged.label_available_at <= merged.origin_at)
        merged.loc[invalid, ["actual_ratio", "actual_capacity"]] = np.nan
        merged.loc[invalid, ["actual_observed_at", "label_available_at"]] = pd.NaT
        parts.append(merged)
    if not parts:
        result = records.copy()
        result["actual_ratio"] = np.nan
        return result
    return pd.concat(parts, ignore_index=True).sort_values(["origin_at", "lot_name"]).reset_index(drop=True)


def temporal_split(dataset):
    """Chronological 60/20/20 split with label-availability embargoes."""
    times = pd.DatetimeIndex(dataset.origin_at.drop_duplicates().sort_values())
    if len(times) < 5:
        raise ValueError("At least five distinct labeled prediction times are needed")
    first = int(len(times) * 0.6)
    second = int(len(times) * 0.8)
    validation_start, test_start = times[first], times[second]
    train = dataset.loc[(dataset.origin_at < validation_start) & (dataset.label_available_at < validation_start)].copy()
    validation = dataset.loc[(dataset.origin_at >= validation_start) & (dataset.origin_at < test_start) & (dataset.label_available_at < test_start)].copy()
    test = dataset.loc[dataset.origin_at >= test_start].copy()
    return train, validation, test
