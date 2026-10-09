"""Public API context snapshots: complete pages and original revision history."""

from datetime import datetime, timedelta, timezone
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
FLIGHTS = "https://apis.data.go.kr/B551177/StatusOfPassengerFlightsDeOdp/"
HOLIDAYS = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"


def request_page(endpoint, key, params):
    url = endpoint + "?" + urlencode({**params, "serviceKey": unquote(key)})
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as response:
                raw = response.read()
            try:
                payload = json.loads(raw)
                response = payload["response"]
                code = str(response["header"]["resultCode"])
                if code not in ("00", "0", "0000"):
                    raise RuntimeError("Public API rejected request (result code " + code[:20] + ")")
                body = response["body"]
                if not isinstance(body, dict):
                    raise ValueError()
                return payload, body
            except (ValueError, KeyError, TypeError):
                raise RuntimeError("Public API returned an invalid or non-JSON response") from None
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Public API HTTP {error.code}; check service authorization") from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("Public API connection failed after three attempts") from None
        time.sleep(2 ** attempt)


def pages(endpoint, key, params):
    records, raw_pages = [], []
    total = None
    for page in range(1, 51):
        raw, body = request_page(endpoint, key, {**params, "pageNo": page, "numOfRows": 1000})
        count = int(body["totalCount"])
        if count < 0 or total is not None and count != total:
            raise ValueError("API pagination total changed; snapshot was not saved")
        total = count
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item", items)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Invalid API item list")
        records.extend(items)
        raw_pages.append(raw)
        if len(records) == total:
            return records, raw_pages
        if not items or len(records) > total:
            raise ValueError("Incomplete API pages; snapshot was not saved")
    raise ValueError("API pagination exceeded 50 pages")


def fetch_flights(key, target_date, direction):
    if direction not in ("arrival", "departure"):
        raise ValueError("Invalid flight direction")
    method = "getPassengerArrivalsDeOdp" if direction == "arrival" else "getPassengerDeparturesDeOdp"
    items, raw = pages(FLIGHTS + method, key, {
        "type": "json", "searchday": target_date.strftime("%Y%m%d"),
        "from_time": "0000", "to_time": "2400", "inqtimechcd": "S", "lang": "K",
    })
    identities = set()
    for item in items:
        # Preserve codeshare rows; the future model must group by masterflightid.
        identity = (str(item.get("fid", "")), str(item.get("flightId", "")))
        if not identity[0] or identity in identities:
            raise ValueError("Missing or repeated flight identity; snapshot was not saved")
        scheduled = str(item.get("scheduleDateTime", ""))
        if len(scheduled) not in (12, 14) or not scheduled.startswith(target_date.strftime("%Y%m%d")):
            raise ValueError("Flight schedule is outside the requested date")
        datetime.strptime(scheduled, "%Y%m%d%H%M" if len(scheduled) == 12 else "%Y%m%d%H%M%S")
        identities.add(identity)
    return snapshot("flights", "data.go.kr:15112968", {
        "target_date": target_date.isoformat(), "direction": direction, "time_basis": "scheduled",
    }, items, raw)


def fetch_holidays(key, year, month):
    items, raw = pages(HOLIDAYS, key, {"_type": "json", "solYear": year, "solMonth": f"{month:02}"})
    identities = set()
    for item in items:
        date = str(item.get("locdate", ""))
        datetime.strptime(date, "%Y%m%d")
        identity = (date, str(item.get("seq", "")), str(item.get("dateName", "")))
        if not date.startswith(f"{year}{month:02}") or item.get("isHoliday") not in ("Y", "N") or identity in identities:
            raise ValueError("Invalid holiday scope or duplicate item")
        identities.add(identity)
    # Zero items is a valid monthly snapshot, not a failed request.
    return snapshot("holidays", "data.go.kr:15012690", {"year": year, "month": month}, items, raw)


def snapshot(kind, source, scope, records, raw):
    return dict(kind=kind, source=source, scope=scope, records=records, raw_response=raw,
                fetched_at=datetime.now(timezone.utc))


def collect_context(command, key, days=2, years=None):
    from airport_parking.context_storage import save_snapshot
    today = datetime.now(KST).date()
    scopes = ((today + timedelta(days=offset), direction) for offset in range(days)
              for direction in ("arrival", "departure")) if command == "collect-flights" else (
                  (year, month) for year in (years or [today.year, today.year + 1]) for month in range(1, 13))
    batches = rows = 0
    for scope in scopes:
        batch = fetch_flights(key, *scope) if command == "collect-flights" else fetch_holidays(key, *scope)
        saved = save_snapshot(batch)
        batches += int(saved)
        rows += len(batch["records"]) if saved else 0
    return batches, rows
