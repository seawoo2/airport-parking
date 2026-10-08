"""PostgreSQL persistence for parking observations."""

import json
import os
from collections.abc import Sequence

import psycopg

from airport_parking.preprocessing.parking import ParkingObservation

SCHEMA = """
CREATE TABLE IF NOT EXISTS parking_observations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL,
    lot_name TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    occupied_spaces INTEGER NOT NULL CHECK (occupied_spaces >= 0),
    total_spaces INTEGER NOT NULL CHECK (total_spaces >= 0),
    raw_item JSONB NOT NULL,
    UNIQUE (source, lot_name, observed_at)
);
CREATE INDEX IF NOT EXISTS parking_observations_observed_at_idx
    ON parking_observations (observed_at DESC);
"""

INSERT = """
INSERT INTO parking_observations
    (source, lot_name, observed_at, occupied_spaces, total_spaces, raw_item)
VALUES (%s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (source, lot_name, observed_at) DO NOTHING
"""


def connection_args() -> dict:
    """Read DB connection settings from the current environment."""
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
        "dbname": os.environ["POSTGRES_DB"],
        "user": os.environ["POSTGRES_USER"],
        "password": os.environ["POSTGRES_PASSWORD"],
        "connect_timeout": 10,
    }


def save_observations(records: Sequence[ParkingObservation]) -> int:
    """Save one API snapshot atomically and return the inserted row count."""
    if not records:
        raise ValueError("저장할 주차 데이터가 없습니다")
    inserted = 0
    with psycopg.connect(**connection_args()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            for record in records:
                cursor.execute(INSERT, (
                    "data.go.kr:15095047",
                    record.lot_name,
                    record.observed_at,
                    record.occupied_spaces,
                    record.total_spaces,
                    json.dumps(record.raw_item, ensure_ascii=False),
                ))
                inserted += cursor.rowcount
    return inserted
