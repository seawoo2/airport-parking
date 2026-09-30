"""Command line entry point for local collection and future scheduling."""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from airport_parking.collectors.parking_api import fetch_parking_status
from airport_parking.collectors.passenger_api import fetch_passenger_forecast
from airport_parking.passenger_storage import save_passenger_forecasts
from airport_parking.preprocessing.parking import normalize_parking_item
from airport_parking.storage import save_observations


def main() -> None:
    parser = argparse.ArgumentParser(description="인천공항 주차·승객예고 데이터 수집")
    parser.add_argument("command", choices=["collect", "collect-passengers"])
    parser.add_argument("--day-offset", type=int, choices=(0, 1), help="승객예고 조회: 오늘 0, 내일 1")
    parser.add_argument("--phase", choices=("manual", "baseline", "recheck"), default="manual")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    if args.command == "collect":
        service_key = os.getenv("AIRPORT_PARKING_SERVICE_KEY", "")
        if not service_key:
            parser.error(".env에 AIRPORT_PARKING_SERVICE_KEY를 설정하세요")
        logging.info("주차 현황 API 요청 시작")
        items = fetch_parking_status(service_key)
        logging.info("API 응답 %d건 수신", len(items))
        records = [normalize_parking_item(item) for item in items]
        logging.info("PostgreSQL 저장 시작")
        inserted = save_observations(records)
        logging.info("주차 현황 수집 완료: 받은 항목 %d건, 새로 저장한 항목 %d건", len(records), inserted)
    elif args.command == "collect-passengers":
        service_key = os.getenv("AIRPORT_PASSENGER_SERVICE_KEY") or os.getenv("AIRPORT_PARKING_SERVICE_KEY", "")
        if not service_key:
            parser.error(".env에 AIRPORT_PASSENGER_SERVICE_KEY 또는 AIRPORT_PARKING_SERVICE_KEY를 설정하세요")
        offsets = (args.day_offset,) if args.day_offset is not None else (0, 1)
        logging.info("승객예고 API 요청 시작: 날짜 오프셋 %s", offsets)
        responses = [fetch_passenger_forecast(service_key, offset) for offset in offsets]
        logging.info("승객예고 API 응답 %d건 수신", sum(len(response.items) for response in responses))
        result = save_passenger_forecasts(responses, phase=args.phase)
        logging.info(
            "승객예고 확인 완료: 저장 %d건, 이전 예고와 다른 값 %d건, 변경 없는 날짜 %d건",
            result.saved_rows, result.changed_rows, result.unchanged_days,
        )
