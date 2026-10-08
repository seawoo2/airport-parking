# 로컬 주차 혼잡도 예측과 평가

서버에서 내려받은 최신 CSV로 각 주차장의 **1시간 후 혼잡률**을 예측합니다. 혼잡률은 주차 대수/총 주차면이며 100%를 넘는 값도 그대로 보존합니다. 전체 면수가 0인 미운영 구역은 제외합니다.

## 실행

로컬 저장소 루트에서 실행합니다. `uv sync`가 완료된 환경을 사용합니다.

```powershell
cd C:\dev\workspace\airport-parking
.venv\Scripts\python.exe -m airport_parking.models.congestion train
```

학습(`train`)과 평가(`evaluate`)는 실행 전에 로컬 누적 저장소의 **최종 성공 동기화 시각**을 확인합니다. 동기화 기록이 없거나 현재 시각보다 1시간 이상 오래되었으면 `local-work/server-sync.json`의 SSH 설정으로 신규 행을 동기화합니다. 새 행이 없는 성공 동기화도 확인 시각을 갱신합니다. 실패하면 분석을 중단합니다.

그 다음 전체 누적 데이터를 로컬에서 CSV로 조립하고 사용합니다. 데이터가 늘지 않았으면 기존 CSV를 재사용합니다. 설정 파일 위치가 다르면 `--sync-config 경로`를 지정하고, 설정의 `output_dir`은 `--data-root`와 같아야 합니다. 과거 데이터로 재현하려는 경우에만 `--snapshot 전체CSV폴더`를 지정합니다. 이 명시적 과거 입력에는 자동 동기화를 적용하지 않습니다.

기본 입력은 `data/raw/server/latest.json`이 가리키는 전체 로컬 데이터셋입니다. 결과는 `data/processed/congestion/실행시각-식별자/`에 저장되고 `data/processed/congestion/latest.json`에서 최신 실행을 확인할 수 있습니다. 기존 실행 결과는 보존합니다.

```powershell
$modelRoot = 'data\processed\congestion'
$latest = Get-Content "$modelRoot\latest.json" -Raw | ConvertFrom-Json
$runDir = Join-Path $modelRoot $latest.run_dir
Get-Content "$runDir\metrics.json"
```

## 생성 파일

| 파일 | 내용 |
|---|---|
| `report.md`, `coverage.png` | 데이터 기간·수집 공백·학습 가능 여부와 분석 규칙 |
| `data_quality.json` | 주차장별 행 수·누락 구간·미운영·초과 점유·면수 변경 |
| `training_examples.csv` | 예측 시점별 입력과 연결 가능한 미래 실제값. 실제값 없는 사례도 포함 |
| `metrics.json` | 학습 준비 판정 또는 검증·테스트 구간별 성능 |
| `next_predictions.csv` | 주차장별 마지막 수집 시각에서 1시간 후의 예측 |
| `test_predictions.csv`, `per_lot_metrics.csv`, `backtest.png` | 데이터가 충분할 때 생성하는 보지 않은 테스트 구간 예측과 주차장별 오차 |
| `model.pkl` | 충분한 데이터로 선택된 학습 모델 또는 기준 예측 메타데이터 |

예측 파일의 `prediction_kind=baseline`은 현재 혼잡도가 유지된다는 기준 예측입니다. `learned`는 검증 구간에서 선택된 학습 모델입니다. **파일이 생성됐다는 것만으로 학습 모델이 준비된 것은 아닙니다.** `metrics.json`의 상태와 `selected_model`을 확인하세요.

`estimated_occupied_spaces`는 예측 혼잡률에 현재 주차면수를 곱한 추정치입니다. 미래에 면수가 바뀌면 이 추정 조건이 달라집니다. `stale_input`은 실행 시점에 마지막 수집값이 20분 이상 오래된 경우입니다. 예측의 기준 시각은 파일의 `origin_at`이며 실행 현재 시각이 아닙니다.

## 학습 방식과 미래 정보 누출 방지

- 입력: 주차장·터미널·종류, 현재 혼잡률과 면수, 10/30/60분 전 관측과 변화, 예측 대상 시간·요일, 입출국 승객예고.
- 예측을 만들 수 있었던 시각은 관측이 DB에 저장된 `collected_at`입니다.
- 과거 변수는 해당 시점까지 저장된 관측만 사용합니다. 긴 공백을 한 행 이전의 값이나 미래 보간값으로 대체하지 않습니다.
- 승객예고는 `fetched_at <= origin_at`인 버전 중 최신 예고만 사용합니다. 당일 예고가 이후 변경됐어도 과거 입력에 반영하지 않습니다.
- 실제 목표값은 예측 시점+60분에 가장 가까운 관측을 ±10분 이내에서 연결합니다. 관측이 없으면 학습하지 않습니다.
- 시간 순서로 60% 학습, 20% 검증, 20% 테스트를 나눕니다. 뒤 구간 시작 이후에 확보된 앞 구간 목표값은 제외합니다.
- 현재값 유지, Ridge 회귀, Random Forest를 비교하고 **검증 MAE**로 선택합니다. 테스트 결과로 모델을 다시 선택하지 않습니다.
- 선택된 학습 모델은 마지막에 실제 목표값을 확보한 전체 사례로 다시 학습해 앞으로의 예측에 사용합니다. 테스트 성능은 이 재학습 전 모델의 결과입니다.

초기 실행 기준은 원본·학습 사례 기간 7일 이상, 학습/검증/테스트의 서로 다른 예측 시각 48/12/12개 이상입니다. 이는 초기 평가를 위한 최소 조건이며 계절이나 공휴일까지 정확하다는 근거가 아닙니다. 부족하면 학습·평가 점수를 만들지 않고 기준 예측만 생성합니다.

기본 목표를 바꾸려면 `--horizon-minutes`를 사용합니다. 예측 시간은 허용 오차보다 길어야 합니다. `--min-span-days`를 낮추면 준비 기준을 완화할 수 있지만 부족한 데이터를 정확한 모델로 바꿔주는 옵션은 아닙니다.

## 예측이 실제로 맞았는지 확인

예측 대상 시각이 지난 뒤 서버 데이터를 다시 동기화하고, **이전에 생성한 예측 파일**을 평가합니다. 새로 학습해 예측을 덮어쓴 뒤 평가하지 않습니다.

```powershell
.venv\Scripts\python.exe -m airport_parking.models.congestion evaluate --predictions "$runDir\next_predictions.csv"
```

`$runDir`는 확인하려는 원래 실행 폴더를 가리켜야 합니다. 로컬 시각과 달리 CSV의 시각은 UTC 오프셋을 포함합니다. 같은 폴더에 `next_predictions.evaluation.csv`와 `next_predictions.evaluation.json`이 생성됩니다.

- `matched_rows`: 실제 관측과 연결된 예측 수.
- `pending_or_missing_rows`: 실제 시각이 아직 도래하지 않았거나 누락된 예측 수. 0점으로 처리하지 않습니다.
- `mae_percentage_points`: 평균 절대 오차. 예측 90%, 실제 85%이면 오차 5%p입니다.
- `rmse_percentage_points`: 큰 오차에 더 많은 영향을 받는 지표.
- `busy_f1`: 혼잡률 90% 이상 여부의 F1. 실제 혼잡 사례가 없으면 해석하기 어려우므로 `actual_busy_rows`도 확인합니다. 90%는 이 프로젝트의 초기 실험 기준입니다.

전체 오차와 주차장별 오차를 함께 확인합니다. 기준 예측 대비 테스트 개선율이 음수면 학습 모델이 더 나빴던 것입니다. 데이터를 더 모으며 다른 날짜에서도 결과가 유지되는지 확인하세요.

## 개발 검증

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_congestion.py -v
```

미래 승객예고 제외, 공백 처리, 초과 점유 보존, 시간 분리, 부족한 실제값 처리, 다음 다운로드로 예측 재평가, 합성 데이터의 학습·검증·테스트 흐름을 확인합니다. 합성 데이터 점수는 실제 프로젝트 성능으로 제시하지 않습니다.
