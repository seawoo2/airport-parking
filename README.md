# 인천공항 주차 혼잡도 예측

인천공항 주차장 데이터를 수집·분석해 주차 혼잡도를 예측하는 프로젝트입니다.

## 프로젝트 구조

```text
airport-parking/
├── src/airport_parking/
│   ├── collectors/       # 공공데이터/API 수집
│   ├── preprocessing/    # 데이터 정제
│   ├── features/         # 예측용 변수 생성
│   └── models/           # 예측 모델
├── notebooks/            # Jupyter 분석
├── data/
│   ├── raw/              # 원본 데이터 (Git 제외)
│   └── processed/        # 가공 데이터 (Git 제외)
├── tests/                # 테스트
├── main.py               # 저장소 루트 실행 진입점
├── pyproject.toml
├── uv.lock
└── README.md
```

## 개발 환경

- Windows
- Python 3.13
- Git, uv
- PostgreSQL, Docker (필요 시)

## 시작하기

```powershell
uv sync
uv run python main.py --help
```

설치된 프로젝트 명령으로도 실행할 수 있습니다.

```powershell
uv run airport-parking --help
```

원본 및 가공 데이터는 저장소에 커밋하지 않습니다. 필요한 데이터는 `data/raw/`와 `data/processed/`에 로컬로 저장하세요.

서버에서 수집하고 로컬에서 분석하는 경우 [서버 데이터 동기화 안내](docs/SERVER_DATA_SYNC.md)에 따라 SSH로 신규 행만 내려받고 Windows 예약 작업을 등록할 수 있습니다. 로컬 SQLite에 누적하고, `scripts/materialize-server-data.py`로 전체 분석용 CSV를 조립합니다. 기존 전체 다운로드는 최초 실행 때 자동으로 가져옵니다.

최신 데이터를 기반으로 1시간 후 주차 혼잡도를 예측하고 시간 순서로 평가하려면 [혼잡도 모델 안내](docs/CONGESTION_MODEL.md)를 참고하세요. 로컬에서 `.venv\Scripts\python.exe -m airport_parking.models.congestion train`을 실행합니다. 학습과 평가는 최종 성공 동기화가 1시간 이상 지났으면 먼저 증분 동기화하고, 전체 로컬 데이터셋을 조립합니다. 데이터가 부족하면 학습 성능을 만들지 않고 현재값 유지 기준 예측과 품질 보고서를 생성합니다.

최종 목표인 익일 시간대별 예측은 [익일 예측·일별 평가 안내](docs/NEXT_DAY_FORECAST.md)를 사용합니다. 서버 17:10 익일 승객예고 수집과 로컬 17:10 동기화, 17:15 예측, 00:10 전날 확정 평가를 연결합니다. 평균·최대·최소 혼잡도를 각각 예측·평가합니다.

## 주차 현황 수집

[공공데이터포털의 인천국제공항공사 주차 정보 API](https://www.data.go.kr/data/15095047/openapi.do)에 활용신청을 하고, 발급된 서비스키를 `.env`의 `AIRPORT_PARKING_SERVICE_KEY`에 설정합니다. 일반 인증키와 인코딩된 인증키 모두 입력할 수 있습니다. 나머지 DB 접속 항목은 [.env.example](.env.example)을 참고하세요. `.env`는 Git에 포함되지 않습니다.

PostgreSQL 컨테이너가 실행 중일 때 한 번 수집합니다.

```powershell
uv run airport-parking collect
```

이 명령은 API의 모든 페이지를 가져와 `parking_observations` 테이블에 저장합니다. 제공처의 `datetm`을 한국시간으로 해석해 PostgreSQL의 시간대 포함 시각으로 저장하며, 같은 주차구역과 관측 시각의 데이터는 중복 저장하지 않습니다. `parkingarea`가 0인 행은 제공처가 미운영으로 정의하므로 그대로 보존하고 혼잡률 계산에서는 제외해야 합니다.

주차 대수(`parking`)가 총 주차면(`parkingarea`)보다 큰 응답도 제공처 원값 그대로 저장합니다. 혼잡률은 100%를 넘을 수 있으므로 분석 단계에서 값을 임의로 제한하지 말고 초과 상태를 함께 해석해야 합니다.

첫 실행 후 저장 내용을 확인하려면:

```powershell
docker compose exec db psql -U airport_app -d airport_parking -c "SELECT lot_name, observed_at, occupied_spaces, total_spaces FROM parking_observations ORDER BY observed_at DESC LIMIT 10;"
```

API 키 발급, 데이터 의미, 구현 상태는 [수집 준비 문서](docs/DATA_COLLECTION_PREPARATION.md)에 기록했습니다.

## 승객예고 수집

[인천국제공항공사 승객예고 API](https://www.data.go.kr/data/15095066/openapi.do)의 활용신청을 완료한 뒤 실행합니다. 기존 서비스키를 사용하며, 별도 키가 있다면 `.env`에 `AIRPORT_PASSENGER_SERVICE_KEY`를 설정합니다.

```powershell
uv run airport-parking collect-passengers
```

오늘과 내일의 T1·T2 입국·출국 **예상 승객 수**를 시간별로 저장합니다. 예고 조회 시각도 저장해 이후 주차 혼잡도 예측 학습에서 당시 실제로 알 수 있었던 예고만 사용할 수 있습니다. 자세한 분석 기준은 [승객예고 수집 문서](docs/PASSENGER_FORECAST_COLLECTION.md)에 기록했습니다.

## 로컬 임시 수집 일정

항공 운항 일정·공휴일의 서버 수집 주기, API 활용 신청, 인증키 설정 및 증분 동기화는 [운항·공휴일 수집 문서](docs/context-collection.md)를 참고하세요.

Windows 작업 스케줄러에 2026-10-08(목)까지의 임시 작업을 등록했습니다. 주차 현황은 10분마다, 승객예고는 11:05·17:05·23:05(한국시간)에 수집합니다. 승객예고 17:05에는 다음 날 자료를 기준본으로 저장하고, 나머지 두 번은 값이 바뀐 경우에만 새 버전을 저장합니다. PC가 켜져 있고 Windows 사용자가 로그인한 동안 실행됩니다. 작업 이름, 실행 로그, 호출량 추정치는 [로컬 수집 일정 문서](docs/LOCAL_COLLECTION_SCHEDULE.md)를 참고하세요.
