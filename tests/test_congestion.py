"""Verify point-in-time features, split boundaries and end-to-end training."""

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from airport_parking.features.congestion import attach_targets, build_features, temporal_split
from airport_parking.models.congestion import evaluate, predict, train


def parking_frame(times, counts=None, capacities=None):
    values = pd.DatetimeIndex(times).as_unit("ns")
    return pd.DataFrame({
        "lot_name": "T1 장기 P1 주차장", "observed_at": values,
        "collected_at": values + pd.Timedelta(seconds=30),
        "occupied_spaces": counts if counts is not None else [50] * len(values),
        "total_spaces": capacities if capacities is not None else [100] * len(values),
    })


def empty_passengers():
    return pd.DataFrame({
        "terminal": pd.Series(dtype=str), "direction": pd.Series(dtype=str),
        "target_hour": pd.Series(dtype="datetime64[ns, UTC]"),
        "fetched_at": pd.Series(dtype="datetime64[ns, UTC]"),
        "expected_passengers": pd.Series(dtype=int),
    })


class FeatureTests(unittest.TestCase):
    def test_forecast_revision_after_prediction_is_not_used(self):
        parking = parking_frame(pd.date_range("2026-10-08T01:00:00Z", periods=1))
        passengers = pd.DataFrame({
            "terminal": ["T1", "T1"], "direction": ["arrival", "arrival"],
            "target_hour": pd.to_datetime(["2026-10-08T02:00:00Z"] * 2, utc=True).as_unit("ns"),
            "fetched_at": pd.to_datetime(["2026-10-08T00:00:00Z", "2026-10-08T01:01:00Z"], utc=True).as_unit("ns"),
            "expected_passengers": [100, 999],
        })
        features = build_features(parking, passengers)
        self.assertEqual(features.iloc[0].passengers_target_arrival, 100)
        self.assertLessEqual(features.iloc[0].latest_forecast_used_at, features.iloc[0].origin_at)

    def test_long_gap_is_not_filled_or_used_as_ten_minute_lag(self):
        parking = parking_frame(pd.to_datetime(["2026-10-08T01:00:00Z", "2026-10-08T13:00:00Z"], utc=True))
        features = build_features(parking, empty_passengers())
        self.assertTrue(pd.isna(features.iloc[1].lag_10_ratio))
        labeled = attach_targets(features, parking)
        self.assertTrue(labeled.actual_ratio.isna().all())

    def test_zero_capacity_is_excluded_and_overcapacity_preserved(self):
        parking = parking_frame(pd.date_range("2026-10-08T01:00:00Z", periods=2, freq="10min"), [110, 10], [100, 0])
        features = build_features(parking, empty_passengers())
        self.assertEqual(len(features), 1)
        self.assertEqual(predict(None, features)[0], 1.1)

    def test_train_and_validation_labels_are_available_before_next_split(self):
        origin = pd.date_range("2026-10-01T00:00:00Z", periods=240, freq="10min").as_unit("ns")
        dataset = pd.DataFrame({"origin_at": origin, "label_available_at": origin + pd.Timedelta(minutes=60)})
        training, validation, testing = temporal_split(dataset)
        self.assertTrue((training.label_available_at < validation.origin_at.min()).all())
        self.assertTrue((validation.label_available_at < testing.origin_at.min()).all())
        self.assertLess(sum(len(part) for part in (training, validation, testing)), len(dataset))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        empty_passengers().to_csv(self.snapshot / "passenger_forecasts.csv", index=False)

    def args(self):
        return argparse.Namespace(data_root=self.root, snapshot=self.snapshot, horizon_minutes=60,
                                  tolerance_minutes=10, busy_threshold=0.9, min_span_days=7,
                                  output_root=self.root / "output")

    def test_sparse_data_generates_baseline_without_fake_training_scores(self):
        parking_frame(pd.date_range("2026-10-08T01:00:00Z", periods=3, freq="10min")).to_csv(self.snapshot / "parking.csv", index=False)
        with patch("airport_parking.models.congestion.coverage_plot"):
            folder = train(self.args())
        assessment = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(assessment["status"], "insufficient_data")
        self.assertNotIn("test_metrics", assessment)
        predictions = pd.read_csv(folder / "next_predictions.csv")
        self.assertTrue(predictions.prediction_kind.eq("baseline").all())
        summary = evaluate(argparse.Namespace(data_root=self.root, snapshot=self.snapshot, predictions=folder / "next_predictions.csv", tolerance_minutes=10, busy_threshold=0.9))
        self.assertEqual(summary["matched_rows"], 0)
        # A later download resolves the originally saved forecast without retraining.
        later = parking_frame(pd.date_range("2026-10-08T01:00:00Z", periods=9, freq="10min"), [50] * 8 + [60])
        later.to_csv(self.snapshot / "parking.csv", index=False)
        summary = evaluate(argparse.Namespace(data_root=self.root, snapshot=self.snapshot, predictions=folder / "next_predictions.csv", tolerance_minutes=10, busy_threshold=0.9))
        self.assertEqual(summary["matched_rows"], 1)
        self.assertAlmostEqual(summary["forecast_metrics"]["mae_percentage_points"], 10)

    def test_full_training_validation_test_and_refit_workflow(self):
        times = pd.date_range("2026-10-01T00:00:00Z", periods=8 * 24 * 6 + 1, freq="10min")
        counts = np.rint(50 + 25 * np.sin(np.arange(len(times)) * 2 * np.pi / 144)).astype(int)
        parking_frame(times, counts).to_csv(self.snapshot / "parking.csv", index=False)
        with patch("airport_parking.models.congestion.coverage_plot"), patch("airport_parking.models.congestion.backtest_plot"):
            folder = train(self.args())
        assessment = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(assessment["status"], "evaluated")
        self.assertEqual(set(assessment["test_metrics"]), {"persistence", "ridge", "random_forest"})
        test = pd.read_csv(folder / "test_predictions.csv")
        self.assertEqual(len(test), assessment["test_metrics"]["persistence"]["rows"])
        self.assertTrue(np.isfinite(test.ridge).all())
        self.assertTrue((folder / "model.pkl").exists())


if __name__ == "__main__":
    unittest.main()
