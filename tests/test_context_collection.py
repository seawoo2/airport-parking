import csv
import io
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from urllib.error import HTTPError

from airport_parking.collectors import context_api as api
from airport_parking import sync
from test_server_sync import archive_bytes, ZERO


class ContextTests(unittest.TestCase):
    def test_pages_preserve_all_items(self):
        with patch.object(api, "request_page", side_effect=[({}, {"totalCount": 2, "items": {"item": {"fid": "1"}}}),
                                                           ({}, {"totalCount": 2, "items": [{"fid": "2"}]})]):
            rows, raw = api.pages("endpoint", "key", {})
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(raw), 2)

    def test_truncated_or_changed_pages_rejected(self):
        for body in ({"totalCount": 3, "items": []}, {"totalCount": 2, "items": []}):
            with patch.object(api, "request_page", side_effect=[({}, {"totalCount": 2, "items": [{"fid": "1"}]}), ({}, body)]):
                with self.assertRaises(ValueError):
                    api.pages("endpoint", "key", {})

    def test_empty_month_is_preserved(self):
        with patch.object(api, "request_page", return_value=({}, {"totalCount": 0, "items": ""})):
            result = api.fetch_holidays("key", 2026, 10)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["scope"], {"year": 2026, "month": 10})

    def test_flight_query_and_duplicate_guard(self):
        row = {"fid": "1", "flightId": "KE1", "scheduleDateTime": "202610101200"}
        with patch.object(api, "pages", return_value=([row], [])) as pages:
            result = api.fetch_flights("key", date(2026, 10, 10), "departure")
            self.assertEqual(pages.call_args.args[2]["inqtimechcd"], "S")
            self.assertEqual(result["scope"]["direction"], "departure")
        with patch.object(api, "pages", return_value=([row, row], [])):
            with self.assertRaises(ValueError):
                api.fetch_flights("key", date(2026, 10, 10), "arrival")

    def test_http_error_does_not_leak_key(self):
        with patch.object(api, "urlopen", side_effect=HTTPError("https://example/?serviceKey=SECRET", 403, "SECRET", {}, None)):
            with self.assertRaises(RuntimeError) as caught:
                api.request_page("https://example/", "SECRET", {})
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_context_ingestion_and_empty_delta(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = sqlite3.connect(":memory:")
            connection.execute("CREATE TABLE contexts(id INTEGER PRIMARY KEY, payload TEXT)")
            path = root / "context_snapshots.csv"
            row = ["1", "data.go.kr:15012690", "holidays", "2026-10-09T00:00:00+00:00", '{"year":2026,"month":10}', "[]"]
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output); writer.writerow(sync.CONTEXT_HEADERS); writer.writerow(row)
            self.assertEqual(sync.ingest_context(connection, root, {path.name: 1}, 0, 1), 1)
            with path.open("w", encoding="utf-8", newline="") as output:
                csv.writer(output).writerow(sync.CONTEXT_HEADERS)
            self.assertEqual(sync.ingest_context(connection, root, {path.name: 0}, 1, 1), 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 1)
            connection.close()

    def test_new_context_cursor_migrates_and_materializes(self):
        def archive(lower, identity, rows):
            upper = {"parking_id": 1, "passenger_batch_id": 2, "context_id": identity}
            original = archive_bytes(lower=lower, upper=upper, parking=None if lower == ZERO else [],
                                     passengers=None if lower == ZERO else [])
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
                for name in source.namelist():
                    data = source.read(name)
                    if name == "manifest.json":
                        manifest = json.loads(data)
                        manifest["rows"]["context_snapshots.csv"] = len(rows)
                        data = json.dumps(manifest).encode()
                    target.writestr(name, data)
                csv_output = io.StringIO(newline="")
                writer = csv.writer(csv_output); writer.writerow(sync.CONTEXT_HEADERS); writer.writerows(rows)
                target.writestr("context_snapshots.csv", csv_output.getvalue())
            return output.getvalue()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = root / "key"; key.write_text("placeholder")
            data = root / "data"
            row = ["1", "data.go.kr:15012690", "holidays", "2026-10-09T00:00:00+00:00", '{"year":2026,"month":10}', "[]"]
            def download(payload):
                def export(host, key, remote, cursors, source, destination):
                    destination.write_bytes(payload)
                return export
            with patch.object(sync, "download_delta", side_effect=download(archive(ZERO, 1, [row]))):
                result = sync.sync_data("ubuntu@localhost", key, "/app", data)
            self.assertEqual(result["cursors"]["context_id"], 1)
            self.assertEqual(result["rows"]["context_snapshots.csv"], 1)
            folder = sync.materialize_dataset(data)
            self.assertTrue((folder / "context_snapshots.csv").is_file())
            with patch.object(sync, "download_delta", side_effect=download(archive(result["cursors"], 1, []))):
                repeated = sync.sync_data("ubuntu@localhost", key, "/app", data)
            self.assertEqual(repeated["rows"]["context_snapshots.csv"], 0)
            self.assertEqual(repeated["data_revision"], result["data_revision"])
            with patch.object(sync, "download_delta", side_effect=download(archive(repeated["cursors"], 2, []))):
                with self.assertRaises(ValueError):
                    sync.sync_data("ubuntu@localhost", key, "/app", data)
            self.assertEqual(sync.synchronization_status(data)["cursors"]["context_id"], 1)


if __name__ == "__main__":
    unittest.main()
