"""Client for the Incheon Airport parking status Open API."""

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

URL = "https://apis.data.go.kr/B551177/StatusOfParking/getTrackingParking"
LOG = logging.getLogger(__name__)


class ParkingAPIError(RuntimeError):
    """The remote API could not provide usable parking data."""


def _get_page(service_key: str, page: int, page_size: int) -> dict:
    query = urlencode({
        # The portal offers both decoded and percent-encoded keys. urlencode
        # must receive the decoded form to avoid encoding '%' a second time.
        "serviceKey": unquote(service_key),
        "numOfRows": page_size,
        "pageNo": page,
        "type": "json",
    })
    request = Request(f"{URL}?{query}", headers={"Accept": "application/json"})

    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read()
            return json.loads(payload)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise ParkingAPIError(f"API HTTP 오류: {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 2:
                raise ParkingAPIError("API 연결 또는 시간 초과 오류") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParkingAPIError("API 응답이 유효한 JSON이 아닙니다") from exc
        LOG.warning("API 일시 오류. %s번째 재시도", attempt + 1)
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _parse_page(payload: dict) -> tuple[list[dict], int]:
    if not isinstance(payload, dict):
        raise ParkingAPIError("API 응답 최상위 구조가 객체가 아닙니다")
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        raise ParkingAPIError("API response 구조가 올바르지 않습니다")
    header = response.get("header", {})
    if not isinstance(header, dict):
        raise ParkingAPIError("API header 구조가 올바르지 않습니다")
    code = str(header.get("resultCode", ""))
    if code != "00":
        raise ParkingAPIError(f"API 결과 오류: {code or '결과코드 없음'} ({header.get('resultMsg', '')})")

    body = response.get("body")
    if not isinstance(body, dict):
        raise ParkingAPIError("API body가 없습니다")
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ParkingAPIError("API 주차 데이터 형식이 올바르지 않습니다")
    try:
        total = int(body["totalCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParkingAPIError("API totalCount가 올바르지 않습니다") from exc
    return items, total


def fetch_parking_status(service_key: str, page_size: int = 100) -> list[dict]:
    """Fetch every page, raising on empty or inconsistent responses."""
    if not service_key:
        raise ValueError("AIRPORT_PARKING_SERVICE_KEY가 필요합니다")
    records: list[dict] = []
    page = 1
    while True:
        page_items, total = _parse_page(_get_page(service_key, page, page_size))
        if total <= 0 or not page_items:
            raise ParkingAPIError("API가 주차 데이터를 반환하지 않았습니다")
        records.extend(page_items)
        if len(records) >= total:
            return records
        if page * page_size >= total or page > 100:
            raise ParkingAPIError("API 페이지 수와 totalCount가 일치하지 않습니다")
        page += 1
