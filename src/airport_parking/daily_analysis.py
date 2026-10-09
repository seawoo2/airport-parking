"""Separate daily prediction and completed-day evaluation jobs."""

import argparse
from datetime import timedelta
import json
import logging
from pathlib import Path
import sys
import tempfile

import pandas as pd

from airport_parking.features.congestion import TIMEZONE, load_snapshot
from airport_parking.features.next_day import build_day, build_training, cutoff_for
from airport_parking.features.context import load_context, coverage
from airport_parking.models.next_day import evaluate_predictions, fit, write_predictions
from airport_parking.sync import DEFAULT_CONFIG, DEFAULT_DATA_ROOT, ROOT, atomic_json, config_values, materialize_dataset, store_lock, sync_data, utc_now

LOG = logging.getLogger(__name__)
DEFAULT_OUTPUT = ROOT / "data/processed/next-day"


def daily_run(data_root=DEFAULT_DATA_ROOT, output_root=DEFAULT_OUTPUT, config_path=DEFAULT_CONFIG,
              now=None, cutoff_time="17:15", sync=True, snapshot=None, mode="predict"):
    current = pd.Timestamp(now or utc_now()).tz_convert("UTC")
    local_day = current.tz_convert(TIMEZONE).date()
    cutoff = cutoff_for(local_day, cutoff_time)
    root = Path(output_root).resolve()
    with store_lock(root, timeout=60):
        # A job missed overnight must not silently issue a morning forecast using a future cutoff.
        if mode == "predict" and current < cutoff:
            result = {"status": "skipped_before_cutoff", "current_at": current.isoformat(), "cutoff_at": cutoff.isoformat()}
            atomic_json(root / "last_prediction_job.json", result)
            return result
        if sync:
            # Daily issuance always refreshes, even if the 17:10 sync was less than an hour ago.
            sync_result = sync_data(*config_values(config_path, data_root))
            LOG.info("Pre-analysis delta downloaded: %s", sync_result["rows"])
        dataset = snapshot or materialize_dataset(Path(data_root))
        parking, passengers, quality = load_snapshot(Path(data_root), Path(dataset))
        # The selected snapshot is available now, not necessarily at the 17:15 issue cutoff.
        evaluation_time = pd.Timestamp(utc_now()).tz_convert("UTC") if now is None else current
        if mode == "evaluate":
            evaluations = []
            for prediction in sorted(root.glob("*/predictions.csv")):
                metadata = json.loads((prediction.parent / "manifest.json").read_text(encoding="utf-8"))
                target_day = pd.Timestamp(metadata["target_date"]).date()
                if target_day < local_day:
                    evaluation_path = prediction.parent / "evaluation.json"
                    previous = json.loads(evaluation_path.read_text(encoding="utf-8")) if evaluation_path.exists() else None
                    if previous and previous["status"] == "final" and previous["coverage_pct"] == 100:
                        continue
                    evaluations.append(evaluate_predictions(prediction, parking, evaluation_time))
            result = {"status": "evaluated" if evaluations else "no_completed_predictions",
                      "evaluated_at": evaluation_time.isoformat(), "evaluations": evaluations}
            atomic_json(root / "last_evaluation_job.json", result)
            return result
        job = {"issue_date": str(local_day), "cutoff_at": cutoff.isoformat()}
        destination = root / str(local_day)
        if (destination / "predictions.csv").exists():
            job.update(status="already_issued", run_dir=str(destination))
            atomic_json(root / "last_prediction_job.json", job)
            return job
        try:
            context = load_context(Path(dataset))
            required_after = cutoff - pd.Timedelta(minutes=5)
            rows = build_day(parking, passengers, local_day, cutoff_time, required_after, context=context)
            for name, status in coverage(rows).items():
                if status["available_rows"] < status["total_rows"]:
                    LOG.warning("Context input missing at forecast cutoff: %s (%s/%s rows available)",
                                name, status["available_rows"], status["total_rows"])
            if (rows.anchor_age_minutes > 20).any():
                raise ValueError("Some parking anchors are more than 20 minutes old at issue cutoff")
            training, skipped = build_training(parking, passengers, cutoff, cutoff_time, context=context)
            with tempfile.TemporaryDirectory(prefix=".analysis-", dir=root) as temporary:
                staging = Path(temporary).resolve()
                if not staging.is_relative_to(root):
                    raise ValueError("Temporary analysis folder is outside output directory")
                folder = staging / "run"
                folder.mkdir()
                assessment, model = fit(training, folder)
                assessment["skipped_training_dates"] = skipped
                forecasts = write_predictions(rows, assessment, model, folder, evaluation_time.isoformat())
                if not training.empty:
                    training.to_csv(folder / "training_examples.csv", index=False, encoding="utf-8-sig")
                manifest = {
                    "format_version": 1, "issue_date": str(local_day),
                    "target_date": str(local_day + timedelta(days=1)), "cutoff_at": cutoff.isoformat(),
                    "generated_at": evaluation_time.isoformat(), "snapshot": str(dataset),
                    "target_definition": "hourly_mean_max_min_ratio", "minimum_observation_slots": 4,
                    "forecast_batch_id": int(rows.forecast_batch_id.iloc[0]),
                    "forecast_fetched_at": rows.forecast_fetched_at.iloc[0].isoformat(),
                    "prediction_rows": len(forecasts), "lots": rows.lot_name.nunique(),
                    "input_parking_rows": quality["parking_rows"],
                    "assessment_status": assessment["status"], "selected_model": assessment["selected_model"],
                    "feature_schema_version": 2, "context_input_rows": len(context),
                    "context_coverage": coverage(rows),
                    "context_used_by_model": bool(forecasts.context_used_by_model.iloc[0]),
                }
                atomic_json(folder / "manifest.json", manifest)
                lines = ["# 익일 시간대별 주차 혼잡도 예측", "",
                         f"- 발행일: {local_day}, 입력 마감: 한국시간 {cutoff_time}",
                         f"- 예측일: {manifest['target_date']}, {manifest['lots']}개 구역 × 24시간",
                         "- 정답: 시간대 평균·최대·최소 혼잡률. 시간대별 서로 다른 10분 구간 4개 이상 필요",
                         f"- 승객예고 배치: {manifest['forecast_batch_id']}, 수집 시각: {manifest['forecast_fetched_at']}",
                         f"- 모델: {assessment['selected_model']}, 판정: {assessment['status']}", "",
                         f"- 운항·공휴일 입력 가용성: {manifest['context_coverage']}",
                         f"- 새 입력을 모델이 실제 사용했는지: {manifest['context_used_by_model']}",
                         f"- 새 입력 모델 준비: {assessment['context_model_reason']}", "",
                         "현재값 유지와 전일·전주 동일 시간 기준 예측을 학습 모델과 비교합니다.",
                         "학습 데이터가 부족하면 전주·전일 동일 시간, 현재값 순으로 사용할 수 있는 기준값을 사용합니다.",
                         "예측 주차 대수는 발행 시점의 면수를 적용한 추정치이며 미래 면수 변경을 보장하지 않습니다.", "",
                         "## 학습 준비", "", *["- " + reason for reason in assessment["reasons"]], "",
                         "## 평가", "", "평가는 예측과 분리해 매일 00:10에 완료된 전날 날짜를 대상으로 실행합니다.",
                         "각 과거 발행 폴더의 evaluation.json과 evaluation.csv를 확인하세요.",
                         "아직 끝나지 않은 시간은 pending, 관측이 부족한 완료 시간은 missing으로 표시합니다."]
                (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
                folder.rename(destination)
            atomic_json(root / "latest.json", manifest | {"run_dir": str(local_day)})
            job.update(status="issued", run_dir=str(destination), manifest=manifest)
            atomic_json(root / "last_prediction_job.json", job)
            LOG.info("Next-day prediction issued: %s", manifest)
            return job
        except Exception as exc:
            job.update(status="forecast_failed", error=str(exc))
            atomic_json(root / "last_prediction_job.json", job)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("predict", "evaluate"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cutoff-time", default="17:15")
    # Reproduction options are explicit and excluded from the registered scheduled action.
    parser.add_argument("--as-of", help="Historical UTC-offset timestamp; requires --snapshot")
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args(argv)
    if bool(args.as_of) != bool(args.snapshot):
        parser.error("Historical reproduction requires both --as-of and --snapshot")
    log_root = ROOT / "logs"
    log_root.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_root / "daily-analysis.log", encoding="utf-8")])
    try:
        result = daily_run(args.data_root, args.output_root, args.config,
                           now=pd.Timestamp(args.as_of) if args.as_of else None,
                           cutoff_time=args.cutoff_time, sync=args.snapshot is None,
                           snapshot=args.snapshot, mode=args.command)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        LOG.exception("Daily evaluation/next-day prediction failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
