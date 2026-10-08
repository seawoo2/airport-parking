"""Incremental SSH downloads, local accumulation and analysis dataset assembly."""

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = ROOT / "data/raw/server"
DEFAULT_CONFIG = ROOT / "local-work/server-sync.json"
LOG = logging.getLogger(__name__)
HEADERS = {
    "parking.csv": ["id", "source", "lot_name", "observed_at", "collected_at", "occupied_spaces", "total_spaces"],
    "passenger_forecasts.csv": ["batch_id", "source", "fetched_at", "target_date", "day_offset", "phase", "changed_count", "target_hour", "terminal", "direction", "expected_passengers"],
}

# Fence current writers briefly before capturing identity limits. Without a fence,
# an earlier allocated identity committed later could be skipped by MAX(id).
# The collectors use ordinary INSERT identities and append-only transactions.
REMOTE_SCRIPT = r'''
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

base = ["sudo", "-n", "docker", "compose", "exec", "-T"]
psql = ["db", "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "airport_app", "-d", "airport_parking"]
fence = """BEGIN;
SET LOCAL lock_timeout = '10s';
LOCK TABLE parking_observations, passenger_forecast_batches, passenger_forecasts IN SHARE MODE;
SELECT json_build_object(
 'parking_id', COALESCE((SELECT MAX(id) FROM parking_observations), 0),
 'passenger_batch_id', COALESCE((SELECT MAX(id) FROM passenger_forecast_batches), 0),
 'source_instance', (SELECT system_identifier::text FROM pg_control_system()) || ':' ||
                    (SELECT oid::text FROM pg_database WHERE datname = current_database()));
COMMIT;"""
started = datetime.now(timezone.utc).isoformat()
result = subprocess.run(base + ["-e", "PGOPTIONS=-c timezone=UTC"] + psql + ["-A", "-t", "-c", fence],
                        cwd=REMOTE_DIR, capture_output=True, check=True, timeout=180)
upper = json.loads(result.stdout.decode("utf-8"))
if EXPECTED_SOURCE is not None and upper["source_instance"] != EXPECTED_SOURCE:
    raise RuntimeError("Server database identity changed; use a new local data directory")
for key in ("parking_id", "passenger_batch_id"):
    if upper[key] < CURSORS[key]:
        raise RuntimeError("Server identity cursor moved backwards; check for database restoration or reset")
queries = {
 "parking.csv": f"SELECT id, source, lot_name, observed_at, collected_at, occupied_spaces, total_spaces FROM parking_observations WHERE id > {CURSORS['parking_id']} AND id <= {upper['parking_id']} ORDER BY id",
 "passenger_forecasts.csv": f"SELECT f.batch_id, b.source, b.fetched_at, b.target_date, b.day_offset, b.phase, b.changed_count, f.target_hour, f.terminal, f.direction, f.expected_passengers FROM passenger_forecasts f JOIN passenger_forecast_batches b ON b.id = f.batch_id WHERE b.id > {CURSORS['passenger_batch_id']} AND b.id <= {upper['passenger_batch_id']} ORDER BY b.id, f.target_hour, f.terminal, f.direction",
}
manifest = {"format_version": 2, "mode": "incremental", "started_at_utc": started,
            "timezone": "UTC", "from_cursors": CURSORS,
            "to_cursors": {key: upper[key] for key in CURSORS},
            "source_instance": upper["source_instance"], "rows": {}}
with tempfile.TemporaryDirectory(prefix="airport-parking-export-") as temp:
    folder = Path(temp)
    for name, query in queries.items():
        with (folder / name).open("wb") as output:
            subprocess.run(base + ["-e", "PGOPTIONS=-c timezone=UTC -c default_transaction_read_only=on"] +
                           psql + ["-c", "COPY (" + query + ") TO STDOUT WITH (FORMAT CSV, HEADER TRUE)"],
                           cwd=REMOTE_DIR, stdout=output, check=True, timeout=180)
        with (folder / name).open(encoding="utf-8", newline="") as exported:
            reader = csv.reader(exported)
            next(reader)
            manifest["rows"][name] = sum(1 for _ in reader)
    manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = folder / "export.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name in (*queries, "manifest.json"):
            zipped.write(folder / name, arcname=name)
    with archive.open("rb") as source:
        shutil.copyfileobj(source, sys.stdout.buffer)
'''


def utc_now():
    return datetime.now(timezone.utc)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def store_lock(root, timeout=660):
    """Serialize scheduler, manual and model downloads; OS releases on exit."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sync.lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another local synchronization is still running")
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def meta_get(connection, key, default=None):
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def meta_set(connection, key, value):
    connection.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value)))


def cursor_values(connection):
    return meta_get(connection, "cursors", {"parking_id": 0, "passenger_batch_id": 0})


def checked_snapshot(root, pointer):
    folder = (root / pointer["dataset_dir"]).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("Snapshot must be inside the local data directory")
    return folder


def csv_rows(folder, name):
    with (folder / name).open(encoding="utf-8-sig", newline="") as exported:
        reader = csv.reader(exported, strict=True)
        if next(reader, None) != HEADERS[name]:
            raise ValueError("Unexpected CSV columns: " + name)
        for row in reader:
            if len(row) != len(HEADERS[name]):
                raise ValueError("Incomplete CSV row: " + name)
            result = dict(zip(HEADERS[name], row))
            identity = "id" if name == "parking.csv" else "batch_id"
            if not result[identity].isdigit() or int(result[identity]) <= 0:
                raise ValueError("Invalid identity: " + name)
            for field in (("occupied_spaces", "total_spaces") if name == "parking.csv" else ("expected_passengers",)):
                if not result[field].isdigit():
                    raise ValueError("Invalid nonnegative count: " + field)
            fields = ("observed_at", "collected_at") if name == "parking.csv" else ("target_hour", "fetched_at")
            for field in fields:
                stamp = datetime.fromisoformat(result[field])
                if stamp.tzinfo is None:
                    raise ValueError("Timestamp must include timezone: " + field)
            if name != "parking.csv" and (result["terminal"] not in ("T1", "T2") or result["direction"] not in ("arrival", "departure")):
                raise ValueError("Unknown forecast terminal or direction")
            yield result


def ingest(connection, folder, counts=None, lower=None, upper=None):
    inserted = {}
    maxima = cursor_values(connection).copy()
    for name in HEADERS:
        count = added = 0
        keys_seen = set()
        for row in csv_rows(folder, name):
            if name == "parking.csv":
                identity = int(row["id"])
                cursor_name = "parking_id"
                table, key, conditions = "parking", (identity,), "id = ?"
            else:
                identity = int(row["batch_id"])
                cursor_name = "passenger_batch_id"
                table, key, conditions = "passengers", (identity, row["target_hour"], row["terminal"], row["direction"]), "batch_id = ? AND target_hour = ? AND terminal = ? AND direction = ?"
            if key in keys_seen:
                raise ValueError("Duplicate row identity in export: " + name)
            keys_seen.add(key)
            if lower is not None and not lower[cursor_name] < identity <= upper[cursor_name]:
                raise ValueError("Row is outside the requested incremental cursor range")
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            existing = connection.execute("SELECT payload FROM " + table + " WHERE " + conditions, key).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError("Existing immutable row changed on server: " + name)
            else:
                placeholders = ",".join("?" for _ in (*key, payload))
                connection.execute("INSERT INTO " + table + " VALUES (" + placeholders + ")", (*key, payload))
                added += 1
            maxima[cursor_name] = max(maxima[cursor_name], identity)
            count += 1
        if counts is not None and count != counts.get(name):
            raise ValueError("Export row count does not match: " + name)
        inserted[name] = added
    return inserted, maxima


@contextmanager
def open_store(root):
    connection = sqlite3.connect(root / "store.sqlite3", timeout=30)
    try:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS parking (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS passengers (
            batch_id INTEGER, target_hour TEXT, terminal TEXT, direction TEXT, payload TEXT NOT NULL,
            PRIMARY KEY(batch_id, target_hour, terminal, direction));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if meta_get(connection, "initialized") is None:
            with connection:
                counts = {name: 0 for name in HEADERS}
                maxima = {"parking_id": 0, "passenger_batch_id": 0}
                if (root / "latest.json").exists():
                    pointer = read_json(root / "latest.json")
                    folder = checked_snapshot(root, pointer)
                    manifest = read_json(folder / "manifest.json")
                    if manifest.get("mode") == "incremental":
                        raise ValueError("Cannot bootstrap from a delta without its cumulative store")
                    counts, maxima = ingest(connection, folder, manifest.get("rows", pointer.get("rows")))
                    if manifest.get("source_instance"):
                        meta_set(connection, "source_instance", manifest["source_instance"])
                    stamp = pointer.get("last_synced_at_utc", pointer.get("downloaded_at_utc"))
                    if stamp:
                        meta_set(connection, "last_synced_at_utc", stamp)
                meta_set(connection, "cursors", maxima)
                meta_set(connection, "data_revision", 1 if sum(counts.values()) else 0)
                meta_set(connection, "initialized", True)
        yield connection
    finally:
        connection.close()


def store_status(connection):
    return {
        "format_version": 2, "cursors": cursor_values(connection),
        "data_revision": meta_get(connection, "data_revision", 0),
        "last_synced_at_utc": meta_get(connection, "last_synced_at_utc"),
        "source_instance": meta_get(connection, "source_instance"),
        "total_rows": {
            "parking.csv": connection.execute("SELECT COUNT(*) FROM parking").fetchone()[0],
            "passenger_forecasts.csv": connection.execute("SELECT COUNT(*) FROM passengers").fetchone()[0],
        },
    }


def download_delta(host, key, remote_dir, cursors, source, destination):
    script = "REMOTE_DIR = " + repr(remote_dir) + "\nCURSORS = " + repr(cursors) + "\nEXPECTED_SOURCE = " + repr(source) + "\n" + REMOTE_SCRIPT
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
               "-i", str(key), host, "python3", "-"]
    with destination.open("wb") as downloaded:
        result = subprocess.run(command, input=script.encode("utf-8"), stdout=downloaded, stderr=subprocess.PIPE, timeout=600)
    if result.returncode:
        raise RuntimeError("SSH export failed: " + result.stderr.decode("utf-8", errors="replace").strip())


def sync_data(host: str, key: Path, remote_dir: str, output_dir: Path) -> dict:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Host must have the form ubuntu@IP-or-DNS-name")
    key = key.expanduser().resolve(strict=True)
    if not key.is_file() or not remote_dir.startswith("/"):
        raise ValueError("SSH key must be a file and remote directory must be absolute")
    root = output_dir.expanduser().resolve()
    with store_lock(root), open_store(root) as connection:
        cursors = cursor_values(connection)
        source = meta_get(connection, "source_instance")
        LOG.info("Downloading delta after parking_id=%s, passenger_batch_id=%s", cursors["parking_id"], cursors["passenger_batch_id"])
        with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as temp:
            staging = Path(temp).resolve()
            if not staging.is_relative_to(root):
                raise ValueError("Temporary directory is outside the output directory")
            archive = staging / "export.zip"
            download_delta(host, key, remote_dir, cursors, source, archive)
            snapshot = staging / "delta"
            snapshot.mkdir()
            with zipfile.ZipFile(archive) as zipped:
                if sorted(zipped.namelist()) != sorted([*HEADERS, "manifest.json"]):
                    raise ValueError("Export archive contains unexpected files")
                for name in (*HEADERS, "manifest.json"):
                    with zipped.open(name) as incoming, (snapshot / name).open("wb") as target:
                        shutil.copyfileobj(incoming, target)
            manifest = read_json(snapshot / "manifest.json")
            if manifest.get("format_version") != 2 or manifest.get("mode") != "incremental" or manifest.get("timezone") != "UTC":
                raise ValueError("Expected a version 2 incremental UTC export")
            if manifest.get("from_cursors") != cursors:
                raise ValueError("Delta starting cursors do not match local state")
            upper = manifest["to_cursors"]
            if set(upper) != set(cursors) or any(type(upper[key]) is not int or upper[key] < value for key, value in cursors.items()):
                raise ValueError("Invalid or regressed server cursors")
            if not manifest.get("source_instance") or source is not None and manifest["source_instance"] != source:
                raise ValueError("Server database identity changed")
            for field in ("started_at_utc", "finished_at_utc"):
                if datetime.fromisoformat(manifest[field]).tzinfo is None:
                    raise ValueError("Export timestamp must include timezone")
            with connection:
                added, maxima = ingest(connection, snapshot, manifest["rows"], cursors, upper)
                for name, cursor_name in (("parking.csv", "parking_id"), ("passenger_forecasts.csv", "passenger_batch_id")):
                    if upper[cursor_name] > cursors[cursor_name] and maxima[cursor_name] != upper[cursor_name]:
                        raise ValueError("Delta is missing its final committed identity: " + name)
                stamp = utc_now().isoformat()
                revision = meta_get(connection, "data_revision", 0) + (1 if sum(added.values()) else 0)
                meta_set(connection, "cursors", upper)
                meta_set(connection, "source_instance", manifest["source_instance"])
                meta_set(connection, "last_synced_at_utc", stamp)
                meta_set(connection, "data_revision", revision)
                name = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                delta_root = root / "deltas"
                delta_root.mkdir(exist_ok=True)
                destination = delta_root / name
                snapshot.rename(destination)
                result = {**store_status(connection), "mode": "incremental", "rows": added,
                          "delta_dir": str(destination.relative_to(root)), "downloaded_at_utc": stamp}
                meta_set(connection, "last_delta", result["delta_dir"])
            atomic_json(root / "sync_state.json", result)
            return result


def materialize_dataset(data_root: Path) -> Path:
    """Assemble all locally accumulated rows; never contact the server."""
    root = data_root.expanduser().resolve()
    with store_lock(root), open_store(root) as connection:
        status = store_status(connection)
        if (root / "latest.json").exists():
            pointer = read_json(root / "latest.json")
            if pointer.get("kind") == "materialized" and pointer.get("data_revision") == status["data_revision"]:
                folder = checked_snapshot(root, pointer)
                if all((folder / name).is_file() for name in (*HEADERS, "manifest.json")):
                    return folder
        if status["total_rows"]["parking.csv"] == 0:
            raise ValueError("No locally synchronized parking data; synchronize first")
        with tempfile.TemporaryDirectory(prefix=".assemble-", dir=root) as temp:
            staging = Path(temp).resolve()
            if not staging.is_relative_to(root):
                raise ValueError("Temporary directory is outside the output directory")
            folder = staging / "dataset"
            folder.mkdir()
            queries = {"parking.csv": "SELECT payload FROM parking ORDER BY id",
                       "passenger_forecasts.csv": "SELECT payload FROM passengers ORDER BY batch_id, target_hour, terminal, direction"}
            for name, query in queries.items():
                with (folder / name).open("w", encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=HEADERS[name])
                    writer.writeheader()
                    for payload, in connection.execute(query):
                        writer.writerow(json.loads(payload))
            manifest = {**status, "kind": "materialized", "mode": "full_local_dataset", "timezone": "UTC",
                        "rows": status["total_rows"], "assembled_at_utc": utc_now().isoformat()}
            atomic_json(folder / "manifest.json", manifest)
            name = utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
            datasets = root / "datasets"
            datasets.mkdir(exist_ok=True)
            destination = datasets / name
            folder.rename(destination)
            atomic_json(root / "latest.json", {**manifest, "dataset_dir": str(destination.relative_to(root))})
            return destination


def synchronization_status(data_root):
    root = Path(data_root).expanduser().resolve()
    with store_lock(root), open_store(root) as connection:
        return store_status(connection)


def config_values(config_path, data_root=None):
    config_path = Path(config_path)
    if not config_path.is_file():
        raise ValueError("SSH configuration is missing; register-server-sync.ps1 creates local-work/server-sync.json")
    config = read_json(config_path)
    configured_root = Path(config.get("output_dir", DEFAULT_DATA_ROOT)).expanduser().resolve()
    if data_root is not None and configured_root != Path(data_root).expanduser().resolve():
        raise ValueError("SSH configuration output_dir does not match the model data directory")
    return config["host"], Path(config["key_path"]), config.get("remote_dir", "/home/ubuntu/airport-parking"), configured_root


def ensure_fresh_dataset(data_root, config_path=None, max_age_seconds=3600, now=None):
    """Refresh stale data or fail, then assemble the complete analysis dataset."""
    root = Path(data_root).expanduser().resolve()
    status = synchronization_status(root)
    current = now or utc_now()
    stamp = status["last_synced_at_utc"]
    last = datetime.fromisoformat(stamp) if stamp else None
    if last is not None and last.tzinfo is None:
        raise ValueError("Last synchronization timestamp must include timezone")
    age = (current - last).total_seconds() if last else None
    if age is None or age < 0 or age >= max_age_seconds:
        LOG.info("Synchronization is missing or at least one hour old; refreshing before analysis")
        sync_data(*config_values(config_path or DEFAULT_CONFIG, root))
    return materialize_dataset(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("sync", help="Download new rows into the local cumulative store")
    download.add_argument("--config", type=Path)
    download.add_argument("--host")
    download.add_argument("--key", type=Path)
    download.add_argument("--remote-dir", default="/home/ubuntu/airport-parking")
    download.add_argument("--output-dir", type=Path, default=DEFAULT_DATA_ROOT)
    for command in ("materialize", "status"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--data-root", "--output-dir", dest="data_root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args(argv)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(logs / "server-sync.log", encoding="utf-8")])
    try:
        if args.command == "sync":
            if args.config or not args.host and not args.key:
                settings = config_values(args.config or DEFAULT_CONFIG)
            else:
                if not args.host or not args.key:
                    parser.error("Provide --host and --key together, or --config")
                settings = args.host, args.key, args.remote_dir, args.output_dir
            result = sync_data(*settings)
            LOG.info("Incremental download complete: new=%s, total=%s", result["rows"], result["total_rows"])
        elif args.command == "materialize":
            result = {"dataset_dir": str(materialize_dataset(args.data_root))}
        else:
            result = synchronization_status(args.data_root)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        LOG.exception("Operation failed; analysis must not continue with stale data")
        return 1


if __name__ == "__main__":
    sys.exit(main())
