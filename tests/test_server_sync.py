"""Check incremental cursors, atomic accumulation, assembly and freshness."""

import ast
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from airport_parking import sync

ZERO = {"parking_id": 0, "passenger_batch_id": 0}


def parking_row(identity=1):
    return [str(identity), "source", "T2 장기\n주차장", "2026-10-08 01:00:00+00", "2026-10-08 01:00:01+00", "4512", "4404"]


def passenger_row(batch):
    return [str(batch), "source", "2026-10-07 08:05:00+00", "2026-10-08", "1", "baseline", "1", "2026-10-08 01:00:00+00", "T2", "arrival", str(10 + batch)]


def csv_text(name, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(sync.HEADERS[name])
    writer.writerows(rows)
    return buffer.getvalue()


def archive_bytes(lower=ZERO, upper=None, parking=None, passengers=None, wrong_count=False, source="test-db"):
    parking = [parking_row()] if parking is None else parking
    passengers = [passenger_row(1), passenger_row(2)] if passengers is None else passengers
    upper = {"parking_id": 1, "passenger_batch_id": 2} if upper is None else upper
    counts = {"parking.csv": len(parking), "passenger_forecasts.csv": len(passengers) + int(wrong_count)}
    stamp = datetime.now(timezone.utc).isoformat()
    manifest = {"format_version": 2, "mode": "incremental", "timezone": "UTC", "from_cursors": lower,
                "to_cursors": upper, "source_instance": source, "rows": counts,
                "started_at_utc": stamp, "finished_at_utc": stamp}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("parking.csv", csv_text("parking.csv", parking).encode("utf-8"))
        zipped.writestr("passenger_forecasts.csv", csv_text("passenger_forecasts.csv", passengers).encode("utf-8"))
        zipped.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "key.pem"
        self.key.write_text("test placeholder")
        self.output = self.root / "data"

    def fake_download(self, payload, expected=ZERO, returncode=0):
        def download(command, **kwargs):
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("BatchMode=yes", command)
            script = kwargs["input"].decode("utf-8")
            requested = ast.literal_eval(script.splitlines()[1].split(" = ", 1)[1])
            self.assertEqual(requested, expected)
            self.assertIn("WHERE id >", script)
            self.assertIn("WHERE b.id >", script)
            kwargs["stdout"].write(payload)
            return SimpleNamespace(returncode=returncode, stderr=b"connection failed" if returncode else b"")
        return download

    def download(self, payload, expected=ZERO, returncode=0):
        with patch.object(sync.subprocess, "run", side_effect=self.fake_download(payload, expected, returncode)):
            return sync.sync_data("ubuntu@127.0.0.1", self.key, "/home/ubuntu/airport-parking", self.output)

    def test_two_deltas_accumulate_and_assembly_preserves_forecast_versions(self):
        first = self.download(archive_bytes())
        assembled = sync.materialize_dataset(self.output)
        checkpoint = first["cursors"]
        second = self.download(archive_bytes(lower=checkpoint, upper={"parking_id": 2, "passenger_batch_id": 3},
                                             parking=[parking_row(2)], passengers=[passenger_row(3)]), expected=checkpoint)
        self.assertEqual(second["rows"], {"parking.csv": 1, "passenger_forecasts.csv": 1})
        self.assertEqual(second["total_rows"], {"parking.csv": 2, "passenger_forecasts.csv": 3, "context_snapshots.csv": 0})
        # Downloading a delta does not rewrite the full dataset.
        self.assertEqual(sync.read_json(self.output / "latest.json")["dataset_dir"], str(assembled.relative_to(self.output)))
        merged = sync.materialize_dataset(self.output)
        with (merged / "parking.csv").open(encoding="utf-8", newline="") as exported:
            rows = list(csv.DictReader(exported))
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertEqual(rows[0]["lot_name"], "T2 장기\n주차장")
        self.assertGreater(int(rows[0]["occupied_spaces"]), int(rows[0]["total_spaces"]))
        with (merged / "passenger_forecasts.csv").open(encoding="utf-8", newline="") as exported:
            self.assertEqual([row["batch_id"] for row in csv.DictReader(exported)], ["1", "2", "3"])
        self.assertEqual(sync.materialize_dataset(self.output), merged)

    def test_no_new_rows_refreshes_sync_time_without_reassembling(self):
        first = self.download(archive_bytes())
        assembled = sync.materialize_dataset(self.output)
        new_time = datetime.now(timezone.utc) + timedelta(hours=2)
        with patch.object(sync, "utc_now", return_value=new_time):
            second = self.download(archive_bytes(lower=first["cursors"], upper=first["cursors"], parking=[], passengers=[]), expected=first["cursors"])
        self.assertEqual(second["rows"], {"parking.csv": 0, "passenger_forecasts.csv": 0})
        self.assertEqual(second["data_revision"], first["data_revision"])
        self.assertEqual(second["last_synced_at_utc"], new_time.isoformat())
        self.assertEqual(sync.materialize_dataset(self.output), assembled)

    def test_transfer_failure_does_not_advance_cursors_or_freshness(self):
        first = self.download(archive_bytes())
        before = sync.synchronization_status(self.output)
        with self.assertRaises(RuntimeError):
            self.download(b"partial", first["cursors"], returncode=255)
        self.assertEqual(sync.synchronization_status(self.output), before)

    def test_invalid_second_csv_rolls_back_first_csv_and_cursors(self):
        first = self.download(archive_bytes())
        before = sync.synchronization_status(self.output)
        payload = archive_bytes(lower=first["cursors"], upper={"parking_id": 2, "passenger_batch_id": 3},
                                parking=[parking_row(2)], passengers=[passenger_row(3)], wrong_count=True)
        with self.assertRaisesRegex(ValueError, "row count"):
            self.download(payload, first["cursors"])
        self.assertEqual(sync.synchronization_status(self.output), before)
        # Retry receives precisely the same delta and succeeds without duplicates.
        result = self.download(archive_bytes(lower=first["cursors"], upper={"parking_id": 2, "passenger_batch_id": 3},
                                            parking=[parking_row(2)], passengers=[passenger_row(3)]), first["cursors"])
        self.assertEqual(result["total_rows"]["parking.csv"], 2)

    def test_existing_full_export_bootstraps_without_redownloading_history(self):
        self.output.mkdir()
        old = self.output / "old"
        old.mkdir()
        with zipfile.ZipFile(io.BytesIO(archive_bytes())) as zipped:
            for name in (*sync.HEADERS, "manifest.json"):
                (old / name).write_bytes(zipped.read(name))
        # Original format has no incremental protocol metadata.
        manifest = {"timezone": "UTC", "rows": {"parking.csv": 1, "passenger_forecasts.csv": 2}}
        (old / "manifest.json").write_text(json.dumps(manifest))
        sync.atomic_json(self.output / "latest.json", {"dataset_dir": "old", "rows": manifest["rows"], "downloaded_at_utc": datetime.now(timezone.utc).isoformat()})
        checkpoint = {"parking_id": 1, "passenger_batch_id": 2}
        result = self.download(archive_bytes(lower=checkpoint, upper={"parking_id": 2, "passenger_batch_id": 3},
                                            parking=[parking_row(2)], passengers=[passenger_row(3)]), checkpoint)
        self.assertEqual(result["total_rows"], {"parking.csv": 2, "passenger_forecasts.csv": 3, "context_snapshots.csv": 0})
        self.assertTrue((old / "parking.csv").exists())

    def test_source_change_and_regressed_cursor_fail_without_overwriting(self):
        first = self.download(archive_bytes())
        before = sync.synchronization_status(self.output)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.download(archive_bytes(lower=first["cursors"], upper=first["cursors"], parking=[], passengers=[], source="other-db"), first["cursors"])
        with self.assertRaisesRegex(ValueError, "regressed"):
            self.download(archive_bytes(lower=first["cursors"], upper=ZERO, parking=[], passengers=[]), first["cursors"])
        self.assertEqual(sync.synchronization_status(self.output), before)

    def test_rows_outside_delta_range_are_rejected(self):
        first = self.download(archive_bytes())
        before = sync.synchronization_status(self.output)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.download(archive_bytes(lower=first["cursors"], upper={"parking_id": 2, "passenger_batch_id": 2},
                                        parking=[parking_row(1)], passengers=[]), first["cursors"])
        self.assertEqual(sync.synchronization_status(self.output), before)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.key = self.root / "key.pem"
        self.key.write_text("test placeholder")
        sync.atomic_json(self.config, {"host": "ubuntu@127.0.0.1", "key_path": str(self.key), "output_dir": str(self.root)})
        self.now = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def test_fresh_sync_only_assembles_locally(self):
        status = {"last_synced_at_utc": (self.now - timedelta(seconds=3599)).isoformat()}
        with patch.object(sync, "synchronization_status", return_value=status), patch.object(sync, "sync_data") as download, patch.object(sync, "materialize_dataset", return_value=self.root) as assemble:
            self.assertEqual(sync.ensure_fresh_dataset(self.root, now=self.now), self.root)
        download.assert_not_called()
        assemble.assert_called_once_with(self.root)

    def test_one_hour_boundary_and_missing_sync_refresh_before_assembly(self):
        for stamp in ((self.now - timedelta(hours=1)).isoformat(), None):
            with self.subTest(stamp=stamp):
                order = []
                with patch.object(sync, "synchronization_status", return_value={"last_synced_at_utc": stamp}), patch.object(sync, "sync_data", side_effect=lambda *args: order.append("sync")), patch.object(sync, "materialize_dataset", side_effect=lambda *args: order.append("assemble")):
                    sync.ensure_fresh_dataset(self.root, self.config, now=self.now)
                self.assertEqual(order, ["sync", "assemble"])

    def test_refresh_failure_stops_before_analysis_dataset_is_loaded(self):
        status = {"last_synced_at_utc": (self.now - timedelta(hours=2)).isoformat()}
        with patch.object(sync, "synchronization_status", return_value=status), patch.object(sync, "sync_data", side_effect=RuntimeError("SSH failed")), patch.object(sync, "materialize_dataset") as assemble:
            with self.assertRaisesRegex(RuntimeError, "SSH failed"):
                sync.ensure_fresh_dataset(self.root, self.config, now=self.now)
        assemble.assert_not_called()

    def test_mismatched_config_data_directory_is_rejected(self):
        sync.atomic_json(self.config, {"host": "ubuntu@127.0.0.1", "key_path": str(self.key), "output_dir": str(self.root / "other")})
        with patch.object(sync, "synchronization_status", return_value={"last_synced_at_utc": None}), patch.object(sync, "sync_data") as download:
            with self.assertRaisesRegex(ValueError, "does not match"):
                sync.ensure_fresh_dataset(self.root, self.config, now=self.now)
        download.assert_not_called()


class ModelFreshnessTests(unittest.TestCase):
    def test_train_and_evaluate_prepare_input_before_loading(self):
        from airport_parking.models import congestion
        args = SimpleNamespace(snapshot=None, data_root=Path("data"), sync_config=Path("config.json"))
        for operation in (congestion.train, congestion.evaluate):
            with self.subTest(operation=operation.__name__), \
                 patch.object(congestion, "ensure_fresh_dataset", side_effect=RuntimeError("refresh failed")) as refresh, \
                 patch.object(congestion, "load_snapshot") as load:
                with self.assertRaisesRegex(RuntimeError, "refresh failed"):
                    operation(args)
                refresh.assert_called_once_with(args.data_root, args.sync_config)
                load.assert_not_called()

    def test_explicit_historical_snapshot_does_not_refresh(self):
        from airport_parking.models import congestion
        args = SimpleNamespace(snapshot=Path("historical"), data_root=Path("data"))
        with patch.object(congestion, "ensure_fresh_dataset") as refresh:
            self.assertEqual(congestion.prepare_dataset(args), args.snapshot)
            refresh.assert_not_called()


class RemoteProtocolTests(unittest.TestCase):
    def test_writer_fence_includes_late_committed_identity(self):
        stdout = SimpleNamespace(buffer=io.BytesIO())
        rows = [parking_row(1), parking_row(3)]
        calls = []

        def psql(command, **kwargs):
            sql = command[-1]
            calls.append(sql)
            if "to_regclass" in sql:
                return SimpleNamespace(stdout=b"f\n")
            if "LOCK TABLE" in sql:
                rows.append(parking_row(2))  # previously uncommitted lower identity
                result = {"parking_id": 3, "passenger_batch_id": 0, "source_instance": "test-db"}
                return SimpleNamespace(stdout=json.dumps(result).encode("utf-8"))
            self.assertIn("default_transaction_read_only=on", " ".join(command))
            if "FROM parking_observations" in sql:
                lower, upper = map(int, re.search(r"id > (\d+) AND id <= (\d+)", sql).groups())
                selected = sorted([row for row in rows if lower < int(row[0]) <= upper], key=lambda row: int(row[0]))
                kwargs["stdout"].write(csv_text("parking.csv", selected).encode("utf-8"))
            else:
                kwargs["stdout"].write(csv_text("passenger_forecasts.csv", []).encode("utf-8"))
            return SimpleNamespace()

        with patch.object(sync.subprocess, "run", side_effect=psql), patch("sys.stdout", stdout):
            exec(sync.REMOTE_SCRIPT, {"REMOTE_DIR": "/unused", "CURSORS": ZERO, "EXPECTED_SOURCE": None})
        self.assertIn("COMMIT", calls[1])
        with zipfile.ZipFile(io.BytesIO(stdout.buffer.getvalue())) as zipped:
            records = list(csv.DictReader(io.StringIO(zipped.read("parking.csv").decode("utf-8"))))
            self.assertEqual([row["id"] for row in records], ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
