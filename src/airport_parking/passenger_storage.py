"""Persist forecast vintages for later as-of analysis."""

import json
from dataclasses import dataclass
from datetime import date, timedelta, timezone

import psycopg

from airport_parking.collectors.passenger_api import ForecastResponse
from airport_parking.preprocessing.passenger import is_summary_item, normalize_forecast_item
from airport_parking.storage import connection_args

SCHEMA = """
CREATE TABLE IF NOT EXISTS passenger_forecast_batches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    day_offset SMALLINT NOT NULL CHECK (day_offset IN (0, 1)),
    raw_response JSONB NOT NULL,
    target_date DATE,
    phase TEXT,
    changed_count INTEGER
);
ALTER TABLE passenger_forecast_batches ADD COLUMN IF NOT EXISTS target_date DATE;
ALTER TABLE passenger_forecast_batches ADD COLUMN IF NOT EXISTS phase TEXT;
ALTER TABLE passenger_forecast_batches ADD COLUMN IF NOT EXISTS changed_count INTEGER;
CREATE TABLE IF NOT EXISTS passenger_forecasts (
    batch_id BIGINT NOT NULL REFERENCES passenger_forecast_batches(id),
    target_hour TIMESTAMPTZ NOT NULL,
    terminal TEXT NOT NULL CHECK (terminal IN ('T1', 'T2')),
    direction TEXT NOT NULL CHECK (direction IN ('arrival', 'departure')),
    expected_passengers INTEGER NOT NULL CHECK (expected_passengers >= 0),
    PRIMARY KEY (batch_id, target_hour, terminal, direction)
);
CREATE INDEX IF NOT EXISTS passenger_forecasts_target_hour_idx
    ON passenger_forecasts (target_hour, terminal, direction);
CREATE INDEX IF NOT EXISTS passenger_forecast_batches_fetched_at_idx
    ON passenger_forecast_batches (fetched_at DESC);
CREATE INDEX IF NOT EXISTS passenger_forecast_batches_target_date_idx
    ON passenger_forecast_batches (target_date, fetched_at DESC);
"""


KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class SaveResult:
    saved_rows: int
    changed_rows: int
    unchanged_days: int


def _latest_forecast(cursor: psycopg.Cursor, target_date: date) -> dict[tuple, int]:
    cursor.execute(
        """SELECT b.id FROM passenger_forecast_batches AS b
           WHERE b.target_date = %s
              OR (b.target_date IS NULL AND EXISTS (
                  SELECT 1 FROM passenger_forecasts AS f
                  WHERE f.batch_id = b.id
                    AND (f.target_hour AT TIME ZONE 'Asia/Seoul')::date = %s
              ))
           ORDER BY b.fetched_at DESC LIMIT 1""",
        (target_date, target_date),
    )
    row = cursor.fetchone()
    if row is None:
        return {}
    cursor.execute(
        """SELECT target_hour, terminal, direction, expected_passengers
           FROM passenger_forecasts WHERE batch_id = %s""",
        (row[0],),
    )
    return {(hour, terminal, direction): count for hour, terminal, direction, count in cursor.fetchall()}


def save_passenger_forecasts(responses: list[ForecastResponse], phase: str = "manual") -> SaveResult:
    """Keep the 17:05 baseline; save later checks only when values change."""
    if not responses or len({response.day_offset for response in responses}) != len(responses):
        raise ValueError("중복되지 않은 날짜의 승객예고 응답이 필요합니다")
    if phase not in {"manual", "baseline", "recheck"}:
        raise ValueError("알 수 없는 승객예고 수집 단계입니다")

    normalized = []
    for response in responses:
        hourly_items = [item for item in response.items if not is_summary_item(item)]
        if len(hourly_items) != 24:
            raise ValueError(f"승객예고 시간대가 24개가 아닙니다: {len(hourly_items)}개")
        forecasts = [record for item in hourly_items for record in normalize_forecast_item(item)]
        target_hours = {(record.target_hour, record.terminal, record.direction) for record in forecasts}
        if len(target_hours) != len(forecasts):
            raise ValueError("승객예고 응답에 중복된 시간대가 있습니다")
        target_dates = {record.target_hour.astimezone(KST).date() for record in forecasts}
        if len(target_dates) != 1:
            raise ValueError("한 응답에 둘 이상의 대상 날짜가 들어 있습니다")
        normalized.append((response, forecasts, target_dates.pop()))

    inserted = 0
    changed = 0
    unchanged_days = 0
    with psycopg.connect(**connection_args()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            for response, forecasts, target_date in normalized:
                previous = _latest_forecast(cursor, target_date)
                current = {
                    (forecast.target_hour, forecast.terminal, forecast.direction): forecast.expected_passengers
                    for forecast in forecasts
                }
                changed_in_day = sum(previous.get(key) != value for key, value in current.items())
                if previous and phase != "baseline" and changed_in_day == 0:
                    unchanged_days += 1
                    continue
                cursor.execute(
                    """INSERT INTO passenger_forecast_batches
                       (source, fetched_at, day_offset, raw_response, target_date, phase, changed_count)
                       VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s) RETURNING id""",
                    (
                        "data.go.kr:15095066",
                        response.fetched_at,
                        response.day_offset,
                        json.dumps(response.payload, ensure_ascii=False),
                        target_date,
                        phase,
                        changed_in_day,
                    ),
                )
                batch_id = cursor.fetchone()[0]
                for forecast in forecasts:
                    cursor.execute(
                        """INSERT INTO passenger_forecasts
                           (batch_id, target_hour, terminal, direction, expected_passengers)
                           VALUES (%s, %s, %s, %s, %s)""",
                        (
                            batch_id,
                            forecast.target_hour,
                            forecast.terminal,
                            forecast.direction,
                            forecast.expected_passengers,
                        ),
                    )
                    inserted += 1
                changed += changed_in_day
    return SaveResult(inserted, changed, unchanged_days)
