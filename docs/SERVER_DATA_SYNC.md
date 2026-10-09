# 서버 데이터의 증분 동기화

수집과 PostgreSQL은 Lightsail에서 실행하고, 분석은 로컬에서 수행합니다. SSH로 **지난 성공 동기화 이후 추가된 행만** 내려받습니다. DB 포트를 외부에 열 필요가 없습니다.

## 준비

- 서버에서 주차와 승객예고를 각각 한 번 이상 수집해 테이블을 생성합니다.
- 로컬에서 `uv sync`를 완료합니다.
- 같은 Windows 계정으로 SSH 접속을 먼저 완료해 서버 지문을 등록하고 개인 키를 비대화형으로 사용할 수 있게 합니다.
- 서버 Ubuntu 사용자가 `sudo -n docker compose`를 실행할 수 있어야 합니다. Docker 기본 `airport_app` DB 사용자의 권한을 전제로 합니다.

## 증분 다운로드

기존에 예약 작업을 등록했다면 설정 파일을 그대로 사용합니다.

```powershell
cd C:\dev\workspace\airport-parking
.venv\Scripts\python.exe scripts\sync-server-data.py --config local-work/server-sync.json
```

설정 파일 없이 직접 지정할 수도 있습니다.

```powershell
.venv\Scripts\python.exe scripts\sync-server-data.py --host ubuntu@서버IP --key "C:\경로\LightsailDefaultKey-ap-northeast-2.pem"
```

최초 실행 시 기존 `latest.json`이 가리키는 전체 CSV를 로컬 누적 저장소로 가져와 마지막 ID를 이어받습니다. 기존 다운로드가 없다면 최초 한 번 전체 이력을 받습니다. 이후 주차는 마지막 `id`, 승객예고는 마지막 `batch_id`보다 큰 행만 전송합니다. 승객예고의 모든 버전과 주차 대수 초과값을 보존합니다.

ID를 기준으로 하므로 늦게 입력된 과거 관측도 포함합니다. 서버에서 짧은 테이블 잠금으로 진행 중인 입력의 커밋을 기다린 뒤 전송 상한 ID를 확정합니다. CSV를 내보내는 동안 잠금은 유지하지 않습니다. 현재 수집기의 행 추가 방식과 배치 단위 트랜잭션을 전제로 하며, 과거 행 수정·삭제를 복제하는 기능은 아닙니다.

다운로드·ZIP·CSV 컬럼·행 수·ID 범위를 확인한 뒤 로컬 SQLite 트랜잭션으로 두 데이터와 체크포인트를 함께 반영합니다. 실패하면 체크포인트와 최종 성공 시각을 유지하여 다음 실행에서 재시도합니다. 동시 실행은 로컬 파일 잠금으로 직렬화합니다. 신규 행이 없어도 성공 시각은 갱신합니다.

| 로컬 경로 (`data/raw/server/` 아래) | 용도 |
|---|---|
| `store.sqlite3` | 전체 누적 데이터, 체크포인트, 최종 성공 시각의 기준 저장소 |
| `sync_state.json` | 사람이 조회할 동기화 결과 요약 |
| `deltas/시각-식별자/` | 실행별 신규 CSV와 전송 메타데이터 |
| `datasets/시각-식별자/` | 별도로 조립한 전체 분석용 CSV |
| `latest.json` | 마지막으로 조립한 전체 데이터셋 위치 |

시각은 UTC입니다. 분석 시 `Asia/Seoul`로 변환합니다. CSV에는 분석용 주요 필드를 내보내므로 원본 API JSON을 포함한 DB 백업을 대체하지 않습니다. 서버 DB의 식별자가 바뀌거나 ID가 뒤로 이동하면 동기화를 중단합니다. 의도적으로 DB를 새로 만든 경우 기존 누적 저장소를 보존하고 새 `--output-dir`과 해당 SSH 설정을 사용합니다.

## 전체 데이터셋 조립 — 로컬에서만 실행

동기화 자체는 전체 CSV를 다시 만들지 않습니다. 필요한 시점에 다음 명령으로 전체 누적 데이터를 조립합니다.

```powershell
.venv\Scripts\python.exe scripts\materialize-server-data.py
```

`parking.csv`, `passenger_forecasts.csv`, `manifest.json`을 만들고 완료 후 `latest.json`을 갱신합니다. 서버 접속은 하지 않습니다. 데이터가 늘지 않았다면 이전 전체 CSV를 재사용합니다. `train`과 `evaluate`는 이 조립 단계를 자동 실행합니다.

현재 상태는 다음 명령으로 확인합니다.

```powershell
.venv\Scripts\python.exe -m airport_parking.sync status
Get-Content logs\server-sync.log -Tail 20
```

`last_synced_at_utc`는 최종 성공 시각, `rows`는 해당 실행의 신규 행 수, `total_rows`는 누적 행 수입니다. 조립 CSV의 생성 시각으로 동기화 최신 여부를 판단하지 않습니다.

## 매일 및 로그인 시 자동 실행

이미 등록한 `AirportParking-ServerSync`는 같은 스크립트를 사용하므로 재등록할 필요가 없습니다. 새로 등록하려면:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\register-server-sync.ps1 -ServerHost ubuntu@서버IP -KeyPath "C:\경로\LightsailDefaultKey-ap-northeast-2.pem" -At 09:00
Get-ScheduledTaskInfo -TaskName AirportParking-ServerSync
```

매일 09:00과 로그인 시 실행합니다. PC가 꺼져 있었다면 다음 실행에서 체크포인트 이후의 데이터를 이어받습니다. 설정은 Git 제외 경로 `local-work/server-sync.json`에 보관합니다. 서버 IP가 바뀌면 설정을 갱신합니다.

학습·평가 실행 시 최종 성공 동기화가 **1시간 이상** 지났거나 기록이 없으면 먼저 증분 동기화하고 전체 데이터셋을 조립합니다. 실패하면 분석을 시작하지 않습니다. 자세한 모델 실행법은 [혼잡도 모델 안내](CONGESTION_MODEL.md)를 참고하세요.

누적 저장소와 최신 전체 CSV를 보존하세요. 과거 전체 CSV와 delta 폴더는 자동 삭제하지 않습니다. `store.sqlite3`가 유지된다면 불필요한 과거 사본을 정리할 수 있습니다.

## 분석 코드에서 사용

조립 이후 다음과 같이 전체 CSV를 읽습니다.

```python
import json
from pathlib import Path
import pandas as pd

root = Path("data/raw/server")
latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
dataset = root / latest["dataset_dir"]
parking = pd.read_csv(dataset / "parking.csv")
passengers = pd.read_csv(dataset / "passenger_forecasts.csv")
parking["observed_at"] = pd.to_datetime(parking["observed_at"], utc=True).dt.tz_convert("Asia/Seoul")
passengers["fetched_at"] = pd.to_datetime(passengers["fetched_at"], utc=True)
passengers["target_hour"] = pd.to_datetime(passengers["target_hour"], utc=True)
```
