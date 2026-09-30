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
uv run python main.py
```

설치된 프로젝트 명령으로도 실행할 수 있습니다.

```powershell
uv run airport-parking
```

원본 및 가공 데이터는 저장소에 커밋하지 않습니다. 필요한 데이터는 `data/raw/`와 `data/processed/`에 로컬로 저장하세요.
