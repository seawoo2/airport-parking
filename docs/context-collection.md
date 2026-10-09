# 항공 운항 일정·공휴일 수집

## API 신청과 인증키

공공데이터포털에서 다음 두 API의 활용 신청이 필요합니다. 기존 주차/승객 API 인증키가 같아도 서비스별 이용 권한이 있어야 합니다.

- [인천국제공항공사_여객기 운항 현황 상세 조회 서비스](https://www.data.go.kr/data/15112968/openapi.do)
- [한국천문연구원_특일 정보](https://www.data.go.kr/data/15012690/openapi.do): 공휴일 조회 `getRestDeInfo`

서버 `/home/ubuntu/airport-parking/.env`에 필요하면 아래 항목을 설정합니다. 빈 값이면 기존 `AIRPORT_PARKING_SERVICE_KEY`를 사용합니다. 키는 로그, Git, 동기화 파일에 포함하지 않습니다.

```dotenv
AIRPORT_FLIGHT_SERVICE_KEY=
HOLIDAY_SERVICE_KEY=
```

권한 승인 후 서버에서 최초 수집을 수행합니다. HTTP 403은 접근 거부이며 이용 권한, 인증키와 승인 반영 여부를 확인해야 합니다.

```bash
cd /home/ubuntu/airport-parking
.venv/bin/airport-parking collect-flights --days 2
.venv/bin/airport-parking collect-holidays
```

## 서버 예약 (Asia/Seoul)

| 데이터 | 주기 | 범위 |
| --- | --- | --- |
| 여객기 운항 일정 | 매일 09:05, 17:10, 23:10 | 오늘·내일, 도착·출발, 전체 터미널 |
| 공휴일 | 매주 월요일 16:40 | 올해·내년, 각 12개월 |

운항 일정은 예정일시 기준(`inqtimechcd=S`)으로 조회합니다. 예정/변경일시, 터미널, 상태, 항공사, 편명, 코드셰어, 마스터 편명 등 원본 필드를 보존합니다. 코드셰어 편을 모두 보존하므로 모델에 사용할 때 `masterflightid` 등을 기준으로 실제 운항 편수를 집계해야 합니다. 날짜 범위는 `--days 1..7`, 공휴일은 `--years 2026 2027`로 지정할 수 있습니다.

페이지가 모두 수신되고 검증된 날짜·방향 또는 연도·월만 개별 트랜잭션으로 저장합니다. 실패한 범위는 저장하지 않으며 명령은 실패 상태로 종료합니다. 그 전에 정상 수집된 범위는 보존됩니다.

운항 일정은 동일 응답도 수집 시점별 스냅샷으로 기록합니다. 공휴일은 월별 최초 응답과 변경된 응답을 기록하며, 공휴일이 없는 달의 정상 응답도 저장합니다. API에 미래 달 정보가 아직 게시되지 않았다면 빈 응답만으로 해당 달에 공휴일이 없다고 단정하면 안 됩니다. 공휴일 목록 외에 주말·요일은 날짜에서 생성할 수 있습니다.

## 저장·증분 동기화

PostgreSQL `context_snapshots`에 순차 ID, 데이터 종류, 출처, 수집 시각(UTC), 조회 범위(JSON), 항목 목록(JSON), 원본 페이지(JSON)를 저장합니다. 기존 주차/승객 테이블은 유지합니다.

기존 동기화 명령에 새 테이블을 포함했습니다. `context_id` 이후의 스냅샷만 다운로드하며 이전 체크포인트도 자동으로 확장됩니다. 테이블이 없는 기존 서버도 지원합니다. 로컬 누적 저장과 전체 데이터 조립 결과에 `context_snapshots.csv`가 포함됩니다. CSV의 `scope`와 `records` 열은 JSON 문자열이며 빈 월과 수정 이력도 구별됩니다. 원본 페이지는 서버 DB에만 저장합니다.

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -m airport_parking.sync sync
.venv\Scripts\python.exe -m airport_parking.sync materialize
```

새 데이터의 모델 입력 연결은 별도 작업입니다. 익일 예측에 사용할 때 반드시 예측 마감 시각 이전에 수집한 스냅샷만 선택해야 합니다. 17:10 수집의 실제 종료 시각은 데이터 양과 API 상태에 따라 달라지므로 17:15 이전 수집 완료 여부를 확인해야 합니다.

## 배포·운영 확인

`scripts/deploy-context-collectors.py`는 로컬 SSH 설정을 사용해 수집 파일 3개를 배포하고 스키마와 cron을 등록합니다. 기존 파일과 crontab은 서버 `local-work/context-deploy/<UTC 시각>/`에 백업합니다. 서버에서 다른 내용으로 수정된 배포 대상 파일은 덮어쓰지 않습니다. `# airport-parking managed context` 작업만 교체하므로 재실행 시 중복 등록되지 않습니다.

```bash
crontab -l
tail -n 30 logs/flights.log
tail -n 30 logs/holidays.log
sudo -n docker compose exec -T db psql -U airport_app -d airport_parking -c \
  "SELECT kind, count(*), max(fetched_at), sum(jsonb_array_length(records)) FROM context_snapshots GROUP BY kind;"
```

2026-10-09 배포 확인: 스키마 생성, 수집 파일 체크섬, cron 등록 및 기존 데이터 증분 동기화를 확인했습니다. 최초 실제 API 요청은 두 서비스 모두 HTTP 403으로 실패했으며 활용 신청/승인 후 실제 응답으로 다시 검증해야 합니다.
