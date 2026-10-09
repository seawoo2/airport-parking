"""Append-only snapshots including empty calendar months and API revisions."""
import json
import psycopg
from airport_parking.storage import connection_args

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_snapshots (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source TEXT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('flights', 'holidays')),
 fetched_at TIMESTAMPTZ NOT NULL,
 scope JSONB NOT NULL,
 records JSONB NOT NULL CHECK (jsonb_typeof(records) = 'array'),
 raw_response JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS context_scope_idx ON context_snapshots (kind, scope, id DESC);
"""


def initialize():
    with psycopg.connect(**connection_args()) as connection:
        connection.execute(SCHEMA)


def save_snapshot(batch):
    with psycopg.connect(**connection_args()) as connection:
        connection.execute(SCHEMA)
        # Serialize manual/scheduled writers for revision comparisons.
        connection.execute("SELECT pg_advisory_xact_lock(15112968)")
        scope = json.dumps(batch["scope"], ensure_ascii=False)
        previous = connection.execute(
            "SELECT records FROM context_snapshots WHERE kind=%s AND scope=%s::jsonb ORDER BY id DESC LIMIT 1",
            (batch["kind"], scope)).fetchone()
        # Flight snapshots must preserve the forecast's information cutoff even
        # when unchanged. Calendar snapshots only need successive revisions.
        if batch["kind"] == "holidays" and previous and previous[0] == batch["records"]:
            return False
        connection.execute(
            "INSERT INTO context_snapshots(source,kind,fetched_at,scope,records,raw_response) "
            "VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
            (batch["source"], batch["kind"], batch["fetched_at"], scope,
             json.dumps(batch["records"], ensure_ascii=False), json.dumps(batch["raw_response"], ensure_ascii=False)))
    return True
