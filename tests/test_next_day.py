"""Check next-day cutoffs, three hourly targets and separated daily jobs."""

from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from airport_parking.daily_analysis import daily_run
from airport_parking.features.next_day import build_day, build_training, cutoff_for, forecast_batch, hourly_truth
from airport_parking.models.next_day import TARGETS, evaluate_predictions, fit, forecast


def parking_data(start="2026-10-08T00:00:00+09:00", days=3):
    times = pd.date_range(start, periods=days * 144, freq="10min").tz_convert("UTC").as_unit("ns")
    return pd.DataFrame({"id": np.arange(len(times)) + 1, "source": "test", "lot_name": "T1 장기 P1 주차장",
                         "observed_at": times, "collected_at": times + pd.Timedelta(seconds=2),
                         "occupied_spaces": 90 + np.arange(len(times)) % 30, "total_spaces": 100})


def passenger_data(target="2026-10-10", fetched="2026-10-09T17:10:10+09:00", batch=1, value=100):
    hours = pd.date_range(pd.Timestamp(target, tz="Asia/Seoul"), periods=24, freq="h").tz_convert("UTC")
    return pd.DataFrame([{"batch_id": batch, "source": "test", "target_date": target, "day_offset": 1,
                          "phase": "baseline", "changed_count": 96, "target_hour": hour,
                          "terminal": terminal, "direction": direction, "expected_passengers": value,
                          "fetched_at": pd.Timestamp(fetched).tz_convert("UTC")}
                         for hour in hours for terminal in ("T1", "T2") for direction in ("arrival", "departure")])


class HourlyTests(unittest.TestCase):
    def test_mean_max_min_preserve_overcapacity_and_require_four_slots(self):
        parking = parking_data(days=1).iloc[:6]
        truth = hourly_truth(parking, pd.Timestamp("2026-10-08T01:00:10+09:00"))
        self.assertAlmostEqual(truth.actual_ratio.iloc[0], 0.925)
        self.assertAlmostEqual(truth.actual_max_ratio.iloc[0], 0.95)
        self.assertAlmostEqual(truth.actual_min_ratio.iloc[0], 0.90)
        parking.loc[parking.index[-1], "occupied_spaces"] = 130
        self.assertEqual(hourly_truth(parking).actual_max_ratio.iloc[0], 1.3)
        self.assertTrue(hourly_truth(parking.iloc[:3]).actual_ratio.isna().all())

    def test_unfinished_hours_and_late_arriving_observations_do_not_score(self):
        parking = parking_data(days=1).iloc[:6].copy()
        cutoff = pd.Timestamp("2026-10-08T00:59:00+09:00")
        self.assertTrue(hourly_truth(parking, cutoff).actual_ratio.isna().all())
        parking["collected_at"] = pd.Timestamp("2026-10-08T02:00:00+09:00").tz_convert("UTC")
        self.assertTrue(hourly_truth(parking, cutoff).empty)

    def test_duplicate_readings_in_one_slot_do_not_meet_coverage(self):
        parking = parking_data(days=1).iloc[:1]
        self.assertTrue(hourly_truth(pd.concat([parking] * 6)).actual_ratio.isna().all())

    def test_extremes_use_all_unique_observations_not_only_one_per_slot(self):
        parking = parking_data(days=1).iloc[:6].copy()
        extra = parking.iloc[:1].copy()
        extra["observed_at"] += pd.Timedelta(minutes=1)
        extra["collected_at"] += pd.Timedelta(minutes=1)
        extra["occupied_spaces"] = 150
        truth = hourly_truth(pd.concat([parking, extra]))
        self.assertEqual(truth.actual_max_ratio.iloc[0], 1.5)
        self.assertEqual(truth.observation_slots.iloc[0], 6)


class FeatureTests(unittest.TestCase):
    def test_fixed_cutoff_24_hours_and_future_revision_excluded(self):
        parking = parking_data()
        passengers = pd.concat([passenger_data(), passenger_data(fetched="2026-10-09T17:16:00+09:00", batch=2, value=999)])
        result = build_day(parking, passengers, date(2026, 10, 9))
        self.assertEqual(len(result), 24)
        self.assertEqual(result.target_hour.dt.tz_convert("Asia/Seoul").dt.hour.tolist(), list(range(24)))
        self.assertTrue(result.arrival.eq(100).all())
        self.assertTrue((result.anchor_collected_at <= result.origin_at).all())
        self.assertTrue((result.forecast_fetched_at <= result.origin_at).all())
        self.assertTrue(result.loc[result.target_hour.dt.tz_convert("Asia/Seoul").dt.hour >= 17, "previous_day_same_hour"].isna().all())
        self.assertGreater(result.lead_hours.max(), 24)

    def test_incomplete_batch_and_stale_scheduled_batch_rejected(self):
        cutoff = cutoff_for(date(2026, 10, 9))
        with self.assertRaisesRegex(ValueError, "complete D\\+1"):
            forecast_batch(passenger_data().iloc[:-1], date(2026, 10, 10), cutoff)
        with self.assertRaises(ValueError):
            forecast_batch(passenger_data(fetched="2026-10-09T17:05:00+09:00"), date(2026, 10, 10), cutoff,
                           required_after=cutoff - pd.Timedelta(minutes=5))

    def test_training_labels_available_by_issue_cutoff_only(self):
        passengers = passenger_data(target="2026-10-09", fetched="2026-10-08T17:10:00+09:00")
        cutoff = cutoff_for(date(2026, 10, 9))
        examples, skipped = build_training(parking_data(), passengers, cutoff)
        self.assertFalse(examples.empty)
        self.assertEqual(skipped, [])
        self.assertTrue((examples.label_available_at <= cutoff).all())
        self.assertTrue((examples.target_hour + pd.Timedelta(hours=1) <= cutoff).all())
        self.assertTrue((examples.forecast_fetched_at <= examples.origin_at).all())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.parking = parking_data()
        self.parking.to_csv(self.snapshot / "parking.csv", index=False)
        passenger_data().to_csv(self.snapshot / "passenger_forecasts.csv", index=False)
        self.output = self.root / "output"

    def run_at(self, instant, mode="predict"):
        return daily_run(self.root, self.output, now=pd.Timestamp(instant), sync=False,
                         snapshot=self.snapshot, mode=mode)

    def test_prediction_then_midnight_evaluation_and_idempotent_forecast(self):
        issued = self.run_at("2026-10-09T17:15:00+09:00")
        self.assertEqual(issued["status"], "issued")
        path = self.output / "2026-10-09/predictions.csv"
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertFalse((path.parent / "evaluation.json").exists())
        result = self.run_at("2026-10-10T00:10:00+09:00", "evaluate")
        self.assertEqual(result["status"], "no_completed_predictions")
        result = self.run_at("2026-10-11T00:10:00+09:00", "evaluate")
        summary = result["evaluations"][0]
        self.assertEqual(summary["status"], "final")
        self.assertEqual(summary["matched_rows"], 24)
        self.assertEqual(set(summary["forecast_metrics"]), {"mean", "max", "min"})
        self.assertEqual(summary["pending_rows"], 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original)
        self.assertEqual(self.run_at("2026-10-09T18:00:00+09:00")["status"], "already_issued")

    def test_before_cutoff_does_not_download_or_issue(self):
        with patch("airport_parking.daily_analysis.sync_data") as sync:
            result = daily_run(self.root, self.output, now=pd.Timestamp("2026-10-09T09:00:00+09:00"))
        self.assertEqual(result["status"], "skipped_before_cutoff")
        sync.assert_not_called()

    def test_scheduled_prediction_forces_sync_and_download_failure_stops_issue(self):
        settings = ("ubuntu@test", self.root / "key", "/server", self.root)
        with patch("airport_parking.daily_analysis.config_values", return_value=settings), \
             patch("airport_parking.daily_analysis.sync_data", return_value={"rows": {}}) as sync:
            result = daily_run(self.root, self.output, now=pd.Timestamp("2026-10-09T17:15:00+09:00"), snapshot=self.snapshot)
        self.assertEqual(result["status"], "issued")
        sync.assert_called_once_with(*settings)
        with patch("airport_parking.daily_analysis.config_values", return_value=settings), \
             patch("airport_parking.daily_analysis.sync_data", side_effect=RuntimeError("SSH failed")), \
             patch("airport_parking.daily_analysis.load_snapshot") as load:
            with self.assertRaisesRegex(RuntimeError, "SSH failed"):
                daily_run(self.root, self.output, now=pd.Timestamp("2026-10-11T00:10:00+09:00"), mode="evaluate")
        load.assert_not_called()

    def test_missing_forecast_or_old_parking_fails_without_predictions(self):
        passenger_data(fetched="2026-10-09T17:05:00+09:00").to_csv(self.snapshot / "passenger_forecasts.csv", index=False)
        with self.assertRaisesRegex(ValueError, "complete D\\+1"):
            self.run_at("2026-10-09T17:15:00+09:00")
        self.assertFalse(list(self.output.glob("*/predictions.csv")))
        passenger_data().to_csv(self.snapshot / "passenger_forecasts.csv", index=False)
        self.parking.loc[self.parking.observed_at < pd.Timestamp("2026-10-09T16:00:00+09:00")].to_csv(self.snapshot / "parking.csv", index=False)
        with self.assertRaisesRegex(ValueError, "20 minutes"):
            self.run_at("2026-10-09T17:15:00+09:00")

    def test_missing_truth_excluded_for_all_three_statistics(self):
        self.run_at("2026-10-09T17:15:00+09:00")
        path = self.output / "2026-10-09/predictions.csv"
        shortened = self.parking.loc[self.parking.observed_at < pd.Timestamp("2026-10-10T12:00:00+09:00")]
        summary = evaluate_predictions(path, shortened, pd.Timestamp("2026-10-11T00:10:00+09:00"))
        self.assertEqual(summary["matched_rows"], 12)
        self.assertEqual(summary["missing_rows"], 12)
        self.assertEqual(summary["forecast_metrics"]["max"]["rows"], 12)

    def test_learned_predictions_obey_min_mean_max_order(self):
        frame = build_day(self.parking, passenger_data(), date(2026, 10, 9))
        class FakeModel:
            def predict(self, features):
                return np.tile([1.2, 1.0, 1.4], (len(features), 1))
        result = forecast("ridge", frame, FakeModel())
        self.assertTrue((result[:, 2] <= result[:, 0]).all())
        self.assertTrue((result[:, 0] <= result[:, 1]).all())
        self.assertTrue((result[:, 0] > 1).all())


class TrainingTests(unittest.TestCase):
    def test_multioutput_training_date_split_and_backtest(self):
        base = build_day(parking_data(), passenger_data(), date(2026, 10, 9))
        parts = []
        for day in pd.date_range("2026-09-01", periods=30, freq="D"):
            frame = base.copy()
            frame["target_date"] = str(day.date())
            frame["origin_at"] = cutoff_for(day.date() - pd.Timedelta(days=1))
            frame["target_hour"] = pd.date_range(day.tz_localize("Asia/Seoul"), periods=24, freq="h").tz_convert("UTC")
            frame["label_available_at"] = frame.target_hour + pd.Timedelta(minutes=50)
            frame["actual_ratio"] = 1 + frame.hour_sin * 0.1
            frame["actual_max_ratio"] = frame.actual_ratio + 0.05
            frame["actual_min_ratio"] = frame.actual_ratio - 0.05
            parts.append(frame)
        with tempfile.TemporaryDirectory() as temporary:
            assessment, model = fit(pd.concat(parts), Path(temporary))
            self.assertEqual(assessment["status"], "evaluated")
            self.assertEqual(set(assessment["test_metrics"]["ridge"]), {"mean", "max", "min"})
            self.assertTrue((Path(temporary) / "backtest_predictions.csv").exists())
            split = assessment["splits"]
            self.assertLess(max(split["train"]["target_dates"]), min(split["validation"]["target_dates"]))
            self.assertLess(max(split["validation"]["target_dates"]), min(split["test"]["target_dates"]))
