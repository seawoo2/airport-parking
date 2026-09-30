"""Validate and normalize parking status records."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class ParkingObservation:
    lot_name: str
    occupied_spaces: int
    total_spaces: int
    observed_at: datetime
    raw_item: dict


def normalize_parking_item(item: dict) -> ParkingObservation:
    """Keep provider values; reject records whose meaning is unclear."""
    try:
        lot_name = str(item["floor"]).strip()
        occupied = int(item["parking"])
        total = int(item["parkingarea"])
        observed = datetime.strptime(str(item["datetm"]), "%Y%m%d%H%M%S.%f")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"주차 데이터 필드가 올바르지 않습니다: {item!r}") from exc
    if not lot_name or occupied < 0 or total < 0 or (total > 0 and occupied > total):
        raise ValueError(f"주차 데이터 값이 범위를 벗어났습니다: {item!r}")
    return ParkingObservation(
        lot_name=lot_name,
        occupied_spaces=occupied,
        total_spaces=total,
        observed_at=observed.replace(tzinfo=KST).astimezone(timezone.utc),
        raw_item=item,
    )
