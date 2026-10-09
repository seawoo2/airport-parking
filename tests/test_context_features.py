from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from airport_parking.features.context import context_for_day, load_context
from airport_parking.features.next_day import build_day, cutoff_for
from airport_parking.models.next_day import fit, evaluate_predictions, write_predictions
from airport_parking.daily_analysis import daily_run
from test_next_day import parking_data, passenger_data


def contexts():
    records = [
        {"flightId": "KE1", "masterflightid": "", "codeshare": "Master", "terminalid": "P03", "scheduleDateTime": "202610100100", "remark": ""},
        {"flightId": "AF1", "masterflightid": "KE1", "codeshare": "Slave", "terminalid": "P03", "scheduleDateTime": "202610100100", "remark": ""},
        {"flightId": "TW1", "codeshare": "Master", "terminalid": "P02", "scheduleDateTime": "202610100200", "remark": "결항"},
        {"flightId": "CX1", "codeshare": "Master", "terminalid": "P01", "scheduleDateTime": "202610100300", "remark": ""},
    ]
    return pd.DataFrame([
        {"id": 1, "kind": "flights", "fetched_at": pd.Timestamp("2026-10-09T17:10:30+09:00"), "scope": {"target_date": "2026-10-10", "direction": "arrival", "time_basis": "scheduled"}, "records": records},
        {"id": 2, "kind": "flights", "fetched_at": pd.Timestamp("2026-10-09T17:10:40+09:00"), "scope": {"target_date": "2026-10-10", "direction": "departure", "time_basis": "scheduled"}, "records": []},
        {"id": 3, "kind": "holidays", "fetched_at": pd.Timestamp("2026-10-09T16:40:00+09:00"), "scope": {"year": 2026, "month": 10}, "records": [{"locdate": 20261010, "isHoliday": "Y", "dateName": "test"}]},
    ])


class ContextFeatureTests(unittest.TestCase):
    def test_codeshare_terminal_cancellation_and_holiday(self):
        features = context_for_day(contexts(), date(2026, 10, 10), cutoff_for("2026-10-09"))
        t1, t2 = features["T1"], features["T2"]
        self.assertEqual(t2.flight_arrival_daily.iloc[0], 1)
        self.assertEqual(t1.flight_arrival_daily.iloc[0], 1)
        self.assertEqual(t1.flight_cancelled_arrival.sum(), 1)
        self.assertTrue(t1.flight_departure.eq(0).all())
        self.assertTrue(t1.is_holiday.eq(1).all())
        self.assertTrue(t1.is_day_off.eq(1).all())
        self.assertEqual(t1.flight_arrival_previous_2h.iloc[4], 1)

    def test_after_cutoff_revisions_never_used(self):
        original = contexts()
        revision = original.copy(deep=True)
        revision["id"] += 100
        revision["fetched_at"] = pd.Timestamp("2026-10-09T17:16:00+09:00")
        revision.at[2, "records"] = []
        revision.at[0, "records"] = []
        frame = context_for_day(pd.concat([original, revision]), date(2026, 10, 10), cutoff_for("2026-10-09"))["T2"]
        self.assertEqual(frame.flight_arrival_daily.iloc[0], 1)
        self.assertEqual(frame.is_holiday.iloc[0], 1)
        rows = build_day(parking_data(), passenger_data(), date(2026, 10, 9), context=pd.concat([original, revision]))
        self.assertTrue((rows.flight_arrival_fetched_at <= rows.origin_at).all())
        self.assertTrue(rows.holiday_available.eq(1).all())

    def test_missing_is_not_zero_and_stale_flights_excluded(self):
        old = contexts()
        old["fetched_at"] = pd.Timestamp("2026-10-08T23:00:00+09:00")
        frame = context_for_day(old, date(2026, 10, 10), cutoff_for("2026-10-09"))["T1"]
        self.assertTrue(frame.flight_arrival.isna().all())
        self.assertTrue(frame.flight_arrival_available.eq(0).all())
        self.assertTrue(frame.holiday_available.eq(1).all())
        empty = context_for_day(None, date(2026, 10, 12), cutoff_for("2026-10-11"))["T1"]
        self.assertTrue(empty.is_holiday.isna().all())
        self.assertTrue(empty.is_day_off.isna().all())

    def test_loader_empty_month_and_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(load_context(root).empty)
            data = contexts()
            data["scope"] = data.scope.map(json.dumps)
            data["records"] = data.records.map(json.dumps)
            data.to_csv(root / "context_snapshots.csv", index=False)
            loaded = load_context(root)
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded.records.iloc[1], [])

    def test_context_models_compared_and_evaluation_groups_persisted(self):
        base = build_day(parking_data(), passenger_data(), date(2026, 10, 9), context=contexts())
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
            root = Path(temporary)
            assessment, model = fit(pd.concat(parts), root)
            self.assertTrue(assessment["context_model_eligible"])
            self.assertEqual(set(assessment["test_metrics"]["ridge_context"]), {"mean", "max", "min"})
            self.assertIn("random_forest_context", assessment["validation_metrics"])
            write_predictions(base, assessment, model, root, cutoff_for("2026-10-09").isoformat())
            summary = evaluate_predictions(root / "predictions.csv", parking_data(),
                                           pd.Timestamp("2026-10-11T00:10:00+09:00"))
            self.assertEqual(summary["matched_rows"], 24)
            self.assertEqual(summary["context_coverage"]["holiday_available"]["available_rows"], 24)
            self.assertIn("is_holiday", summary["context_groups"])
            self.assertTrue((root / "evaluation.json").exists())

    def test_daily_prediction_and_midnight_evaluation_with_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"; snapshot.mkdir()
            parking_data().to_csv(snapshot / "parking.csv", index=False)
            passenger_data().to_csv(snapshot / "passenger_forecasts.csv", index=False)
            data = contexts()
            data["scope"] = data.scope.map(json.dumps)
            data["records"] = data.records.map(json.dumps)
            data.to_csv(snapshot / "context_snapshots.csv", index=False)
            output = root / "output"
            result = daily_run(root, output, sync=False, snapshot=snapshot,
                               now=pd.Timestamp("2026-10-09T17:15:00+09:00"))
            self.assertEqual(result["status"], "issued")
            self.assertEqual(result["manifest"]["feature_schema_version"], 2)
            self.assertEqual(result["manifest"]["context_coverage"]["holiday_available"]["available_rows"], 24)
            self.assertFalse(result["manifest"]["context_used_by_model"])
            evaluated = daily_run(root, output, sync=False, snapshot=snapshot, mode="evaluate",
                                  now=pd.Timestamp("2026-10-11T00:10:00+09:00"))
            self.assertEqual(evaluated["evaluations"][0]["matched_rows"], 24)
            self.assertIn("is_holiday", evaluated["evaluations"][0]["context_groups"])


if __name__ == "__main__":
    unittest.main()
