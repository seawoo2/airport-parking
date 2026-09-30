"""Normalize hourly passenger forecasts from the public API."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

KST = timezone(timedelta(hours=9))
TOTAL_FIELDS = {
    ("T1", "arrival"): "t1egsum1",
    ("T1", "departure"): "t1dgsum1",
    ("T2", "arrival"): "t2egsum1",
    ("T2", "departure"): "t2dgsum2",
}


@dataclass(frozen=True)
class PassengerForecast:
    target_hour: datetime
    terminal: str
    direction: str
    expected_passengers: int


def is_summary_item(item: dict) -> bool:
    """The API appends one non-hourly daily total row to each day."""
    date_text = str(item.get("adate", "")).strip()
    time_text = str(item.get("atime", "")).strip()
    return bool(date_text and date_text == time_text and not date_text.isdigit())


def normalize_forecast_item(item: dict) -> list[PassengerForecast]:
    """Convert one provider hour into four terminal/direction totals."""
    try:
        day = datetime.strptime(str(item["adate"]), "%Y%m%d")
        hour_text = str(item["atime"])
        start_text, end_text = hour_text.split("_", 1)
        start_hour, end_hour = int(start_text), int(end_text)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"승객예고 날짜/시간 형식이 올바르지 않습니다: {item!r}") from exc
    if not 0 <= start_hour <= 23 or end_hour not in {start_hour + 1, (start_hour + 1) % 24}:
        raise ValueError(f"승객예고 시간대가 한 시간이 아닙니다: {hour_text}")
    target_hour = day.replace(hour=start_hour, tzinfo=KST).astimezone(timezone.utc)

    records = []
    for (terminal, direction), field in TOTAL_FIELDS.items():
        try:
            value = Decimal(str(item[field]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ValueError(f"승객예고 {field} 값이 없습니다: {item!r}") from exc
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            raise ValueError(f"승객예고 {field} 값이 정수 인원수가 아닙니다: {value}")
        records.append(PassengerForecast(target_hour, terminal, direction, int(value)))
    return records
