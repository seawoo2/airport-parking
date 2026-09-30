"""Incheon Airport hourly passenger forecast API client."""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

URL = "https://apis.data.go.kr/B551177/passgrAnncmt/getPassgrAnncmt"
KST = timezone(timedelta(hours=9))


class PassengerAPIError(RuntimeError):
    """The passenger forecast API returned an unusable response."""


def _safe_http_detail(error: HTTPError, service_key: str) -> str:
    """Read a short upstream error while removing credentials and URLs."""
    detail = error.read(2048).decode("utf-8", errors="replace")
    for key_form in (service_key, unquote(service_key), quote(unquote(service_key), safe="")):
        detail = detail.replace(key_form, "[redacted]")
    detail = re.sub(r"https?://[^\s\"'<>]+", "[url redacted]", detail)
    return " ".join(detail.split())[:300]


@dataclass(frozen=True)
class ForecastResponse:
    day_offset: int
    fetched_at: datetime
    payload: dict
    items: list[dict]


def fetch_passenger_forecast(service_key: str, day_offset: int) -> ForecastResponse:
    """Fetch a selected forecast day, preserving its raw response."""
    if day_offset not in (0, 1):
        raise ValueError("이 API의 day_offset은 0 또는 1이어야 합니다")
    if not service_key:
        raise ValueError("승객예고 API 서비스키가 필요합니다")

    query = urlencode({
        "serviceKey": unquote(service_key),
        "selectdate": day_offset,
        "type": "json",
        "numOfRows": 100,
        "pageNo": 1,
    })
    request = Request(f"{URL}?{query}", headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.load(response)
            fetched_at = datetime.now(timezone.utc)
            break
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                detail = _safe_http_detail(exc, service_key)
                raise PassengerAPIError(f"승객예고 API HTTP 오류: {exc.code}; {detail}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 2:
                raise PassengerAPIError("승객예고 API 연결 또는 시간 초과 오류") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise PassengerAPIError("승객예고 API 응답이 유효한 JSON이 아닙니다") from exc
        time.sleep(2 ** attempt)
    else:
        raise AssertionError("unreachable")

    if not isinstance(payload, dict):
        raise PassengerAPIError("승객예고 API 최상위 응답이 객체가 아닙니다")
    data = payload.get("response", payload)
    if not isinstance(data, dict):
        raise PassengerAPIError("승객예고 API response 구조가 올바르지 않습니다")
    header = data.get("header", {})
    if not isinstance(header, dict) or str(header.get("resultCode", "")) != "00":
        code = header.get("resultCode", "결과코드 없음") if isinstance(header, dict) else "header 오류"
        raise PassengerAPIError(f"승객예고 API 결과 오류: {code}")
    body = data.get("body", {})
    if not isinstance(body, dict):
        raise PassengerAPIError("승객예고 API body 구조가 올바르지 않습니다")
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        raise PassengerAPIError("승객예고 API가 시간대별 데이터를 반환하지 않았습니다")
    expected_day = (fetched_at.astimezone(KST).date() + timedelta(days=day_offset)).strftime("%Y%m%d")
    hourly_dates = {str(item.get("adate", "")) for item in items if str(item.get("adate", "")).isdigit()}
    if hourly_dates != {expected_day}:
        raise PassengerAPIError(f"승객예고 응답 날짜가 요청과 다릅니다: 요청 {expected_day}, 응답 {sorted(hourly_dates)}")
    return ForecastResponse(day_offset, fetched_at, payload, items)
