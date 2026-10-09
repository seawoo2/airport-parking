"""Train and evaluate next-day hourly forecasts with daily point-in-time inputs."""

from pathlib import Path
import pickle

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from airport_parking.features.congestion import TIMEZONE, timestamps
from airport_parking.features.next_day import CATEGORICAL, NUMERIC, FEATURES, hourly_truth
from airport_parking.models.congestion import metrics
from airport_parking.sync import atomic_json

TARGETS = ["actual_ratio", "actual_max_ratio", "actual_min_ratio"]
PREDICTED = ["predicted_ratio", "predicted_max_ratio", "predicted_min_ratio"]
STATISTICS = ("mean", "max", "min")


def estimator(kind):
    numeric = Pipeline([("missing", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                        ("scale", StandardScaler())])
    inputs = ColumnTransformer([
        ("numeric", numeric, NUMERIC),
        ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL),
    ])
    model = Ridge(alpha=10) if kind == "ridge" else RandomForestRegressor(
        n_estimators=150, max_depth=14, min_samples_leaf=6, random_state=42, n_jobs=1)
    return Pipeline([("inputs", inputs), ("regressor", model)])


def forecast(kind, frame, model=None):
    if model is not None:
        values = np.maximum(model.predict(frame[FEATURES]), 0)
    elif kind == "persistence":
        values = np.column_stack([frame.current_ratio.to_numpy()] * 3)
    else:
        # Same-time historical values are known at the issue cutoff.
        pairs = (("previous_week_same_hour", "previous_day_same_hour"),
                 ("previous_week_max", "previous_day_max"), ("previous_week_min", "previous_day_min"))
        values = np.column_stack([frame[week].fillna(frame[day]).fillna(frame.current_ratio).to_numpy() for week, day in pairs])
    # Bound impossible negative values and enforce min <= mean <= max.
    values[:, 1] = np.maximum(values[:, 1], values[:, 0])
    values[:, 2] = np.minimum(values[:, 2], values[:, 0])
    return values


def statistic_metrics(frame, predicted, busy_threshold=0.9):
    return {name: metrics(frame[target], predicted[:, index], busy_threshold)
            for index, (name, target) in enumerate(zip(STATISTICS, TARGETS))}


def fit(dataset, output, busy_threshold=0.9):
    assessment = {"status": "insufficient_data", "selected_model": "seasonal", "labeled_rows": len(dataset),
                  "minimum_days": {"train": 14, "validation": 7, "test": 7}, "reasons": []}
    models = {"persistence": None, "seasonal": None}
    if dataset.empty:
        assessment["reasons"].append("No eligible next-day hourly training examples")
        days = []
    else:
        # Evaluate complete target days. Missing lot/hour labels are never filled.
        hours = dataset.groupby("target_date").target_hour.nunique()
        days = sorted(hours.loc[hours == 24].index)
    assessment["complete_target_days"] = len(days)
    if len(days) < 28:
        assessment["reasons"].append(f"Only {len(days)} complete target days; need at least 28 (14/7/7)")
    if assessment["reasons"]:
        return assessment, None
    train_days, validation_days, test_days = days[:-14], days[-14:-7], days[-7:]
    pieces = [dataset.loc[dataset.target_date.isin(selected)].copy()
              for selected in (train_days, validation_days, test_days)]
    training, validation, testing = pieces
    # Labels must already exist when the first held-out daily forecast is issued.
    training = training.loc[training.label_available_at <= validation.origin_at.min()]
    validation = validation.loc[validation.label_available_at <= testing.origin_at.min()]
    assessment["splits"] = {name: {"days": frame.target_date.nunique(), "rows": len(frame),
                                   "target_dates": sorted(frame.target_date.unique())}
                            for name, frame in zip(("train", "validation", "test"), (training, validation, testing))}
    if training.target_date.nunique() < 14 or validation.target_date.nunique() < 7:
        assessment["reasons"].append("Insufficient days after enforcing label availability at split boundaries")
        return assessment, None
    for name in ("ridge", "random_forest"):
        models[name] = estimator(name).fit(training[FEATURES], training[TARGETS])
    assessment["validation_metrics"] = {name: statistic_metrics(validation, forecast(name, validation, model), busy_threshold)
                                        for name, model in models.items()}
    assessment["selection_rule"] = "Mean of mean/max/min validation MAE; equal weights"
    selected = min(models, key=lambda name: np.mean([value["mae_percentage_points"] for value in assessment["validation_metrics"][name].values()]))
    results = testing[["target_date", "lot_name", "target_hour", *TARGETS]].copy()
    assessment["test_metrics"] = {}
    for name, model in models.items():
        values = forecast(name, testing, model)
        for index, statistic in enumerate(STATISTICS):
            results[f"{name}_{statistic}"] = values[:, index]
        assessment["test_metrics"][name] = statistic_metrics(testing, values, busy_threshold)
    results.to_csv(output / "backtest_predictions.csv", index=False, encoding="utf-8-sig")
    assessment.update(status="evaluated", selected_model=selected)
    fitted = estimator(selected).fit(dataset[FEATURES], dataset[TARGETS]) if selected in ("ridge", "random_forest") else None
    return assessment, fitted


def write_predictions(rows, assessment, model, output, generated_at):
    data = rows.copy()
    kind = assessment["selected_model"]
    values = forecast(kind, data, model)
    for index, column in enumerate(PREDICTED):
        data[column] = values[:, index]
    data["predicted_max_occupancy_pct"] = data.predicted_max_ratio * 100
    data["predicted_min_occupancy_pct"] = data.predicted_min_ratio * 100
    data["predicted_occupancy_pct"] = data.predicted_ratio * 100
    data["estimated_occupied_spaces"] = np.rint(data.predicted_ratio * data.current_capacity).astype(int)
    data["predicted_busy"] = data.predicted_ratio >= 0.9
    data["predicted_max_busy"] = data.predicted_max_ratio >= 0.9
    data["model"] = kind
    data["prediction_kind"] = "learned" if model is not None else "baseline"
    data["generated_at"] = generated_at
    data["target_definition"] = "hourly_mean_max_min_ratio"
    data.to_csv(output / "predictions.csv", index=False, encoding="utf-8-sig")
    with (output / "model.pkl").open("wb") as serialized:
        pickle.dump({"estimator": model, "assessment": assessment, "features": FEATURES}, serialized)
    atomic_json(output / "metrics.json", assessment)
    return data


def evaluate_predictions(predictions_path, parking, as_of, minimum_samples=4, persist=True):
    predictions_path = Path(predictions_path)
    original = pd.read_csv(predictions_path)
    if original.empty or original.target_definition.ne("hourly_mean_max_min_ratio").any():
        raise ValueError("Expected next-day hourly mean/max/min predictions")
    original["target_hour"] = timestamps(original.target_hour)
    if original.duplicated(["lot_name", "target_hour"]).any():
        raise ValueError("Duplicate lot/hour predictions")
    truth = hourly_truth(parking, as_of, minimum_samples)
    checked = original.merge(truth, on=["lot_name", "target_hour"], how="left", validate="one_to_one")
    finished = checked.target_hour + pd.Timedelta(hours=1) <= as_of
    checked["evaluation_status"] = np.where(~finished, "pending", np.where(checked.actual_ratio.notna(), "matched", "missing"))
    valid = checked.loc[checked.evaluation_status == "matched"]
    for target, predicted, statistic in zip(TARGETS, PREDICTED, STATISTICS):
        checked[f"{statistic}_absolute_error_percentage_points"] = (checked[predicted] - checked[target]).abs() * 100
    day_end = checked.target_hour.max() + pd.Timedelta(hours=1)
    summary = {
        "prediction_file": str(predictions_path), "evaluated_at_utc": as_of.isoformat(),
        "issue_date": str(checked.issue_date.iloc[0]), "target_date": str(checked.target_date.iloc[0]),
        "status": "partial" if as_of < day_end else "final",
        "prediction_rows": len(checked), "matched_rows": len(valid),
        "pending_rows": int((checked.evaluation_status == "pending").sum()),
        "missing_rows": int((checked.evaluation_status == "missing").sum()),
        "coverage_pct": len(valid) / len(checked) * 100, "minimum_observation_slots": minimum_samples,
        "target_definition": "hourly_mean_max_min_ratio",
    }
    if len(valid):
        summary["forecast_metrics"] = statistic_metrics(valid, valid[PREDICTED].to_numpy())
        summary["persistence_metrics"] = statistic_metrics(valid, forecast("persistence", valid))
        summary["per_hour"] = {str(hour): statistic_metrics(group, group[PREDICTED].to_numpy())
                               for hour, group in valid.groupby(valid.target_hour.dt.tz_convert(TIMEZONE).dt.hour)}
        summary["per_lot"] = {name: statistic_metrics(group, group[PREDICTED].to_numpy())
                              for name, group in valid.groupby("lot_name")}
    if persist:
        # Forecasts are immutable. Evaluation is replaced atomically as labels arrive.
        temporary = predictions_path.parent / ".evaluation.csv.tmp"
        checked.to_csv(temporary, index=False, encoding="utf-8-sig")
        temporary.replace(predictions_path.parent / "evaluation.csv")
        atomic_json(predictions_path.parent / "evaluation.json", summary)
    return summary
