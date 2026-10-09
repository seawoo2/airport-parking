"""Train, backtest and verify one-hour-ahead parking congestion forecasts."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import uuid

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib import font_manager
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import f1_score, mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from zoneinfo import ZoneInfo

from airport_parking.features.congestion import (
    CATEGORICAL, FEATURES, NUMERIC, TIMEZONE, attach_targets, build_features,
    load_snapshot, temporal_split, timestamps,
)
from airport_parking.sync import ensure_fresh_dataset

ROOT = Path(__file__).resolve().parents[3]


def estimator(kind):
    numeric = Pipeline([("missing", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                        ("scale", StandardScaler())])
    inputs = ColumnTransformer([
        ("numeric", numeric, NUMERIC),
        ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CATEGORICAL),
    ])
    model = Ridge(alpha=10) if kind == "ridge" else RandomForestRegressor(
        n_estimators=150, max_depth=14, min_samples_leaf=6, random_state=42, n_jobs=1,
    )
    return Pipeline([("inputs", inputs), ("regressor", model)])


def predict(model, records):
    values = records.current_ratio.to_numpy() if model is None else model.predict(records[FEATURES])
    # Preserve ratios above 100%; only physically impossible negative predictions are bounded.
    return np.maximum(values, 0)


def metrics(actual, predicted, busy_threshold=0.9):
    return {
        "rows": len(actual),
        "mae_percentage_points": float(mean_absolute_error(actual, predicted) * 100),
        "rmse_percentage_points": float(np.sqrt(mean_squared_error(actual, predicted)) * 100),
        "busy_f1": float(f1_score(np.asarray(actual) >= busy_threshold, np.asarray(predicted) >= busy_threshold, zero_division=0)),
        "actual_busy_rows": int((np.asarray(actual) >= busy_threshold).sum()),
        "busy_threshold_pct": busy_threshold * 100,
    }


def coverage_plot(parking, path):
    available = {font.name for font in font_manager.fontManager.ttflist}
    for name in ("Malgun Gothic", "Noto Sans CJK KR", "NanumGothic"):
        if name in available:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False
    lots = sorted(parking.lot_name.unique())
    figure, axis = plt.subplots(figsize=(12, max(4, len(lots) * 0.3)))
    for index, name in enumerate(lots):
        group = parking.loc[parking.lot_name == name]
        axis.scatter(group.observed_at.dt.tz_convert(TIMEZONE), np.full(len(group), index), s=18, color="#2171b5")
    axis.set_yticks(range(len(lots)), lots, fontsize=8)
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=ZoneInfo(TIMEZONE)))
    axis.set_title("Parking observations (each dot is a recorded observation)")
    axis.set_xlabel("Asia/Seoul")
    axis.grid(axis="x", alpha=0.25)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def backtest_plot(results, selected, path):
    daily = results.copy()
    daily["hour"] = daily.origin_at.dt.tz_convert(TIMEZONE).dt.floor("h")
    summary = daily.groupby("hour")[["actual_ratio", "persistence", selected]].mean() if selected != "persistence" else daily.groupby("hour")[["actual_ratio", "persistence"]].mean()
    # Aggregate test predictions rather than imply a continuous series across outages.
    figure, axis = plt.subplots(figsize=(11, 4))
    for column in summary:
        axis.plot(summary.index, summary[column] * 100, marker=".", label=column)
    axis.set_ylabel("Mean occupancy (%)")
    axis.set_title("Held-out test predictions: hourly means across available lots")
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=ZoneInfo(TIMEZONE)))
    axis.legend()
    axis.grid(alpha=0.2)
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def write_report(folder, quality, assessment, forecasts):
    ready = assessment["status"] == "evaluated"
    lines = ["# 주차 혼잡도 예측 보고서", "",
             "예측 목표는 각 주차장의 예측 시점으로부터 " + str(assessment["horizon_minutes"]) + "분 후 혼잡률입니다.", "",
             f"- 원본 관측: {quality['parking_rows']}행, {quality['lots']}개 주차구역",
             f"- 기간: {quality['first_observed_at']} ~ {quality['last_observed_at']} (UTC)",
             f"- 기간 길이: {quality['span_days']:.2f}일",
             f"- 미래 실제값과 연결된 사례: {assessment['labeled_rows']}행",
             f"- 판정: {'학습 및 시간 순서 평가 완료' if ready else '학습·평가 데이터 부족'}", "",
             "![수집 시점](coverage.png)", ""]
    if not ready:
        lines += ["## 현재 한계", "", *["- " + reason for reason in assessment["reasons"]], "",
                  "학습 모델의 성능 수치를 만들지 않았습니다. 현재 예측은 관측 혼잡률이 유지된다는 기준 예측입니다.", ""]
    else:
        lines += ["## 평가 결과", "", "모델은 검증 구간의 MAE로 선택했습니다. 테스트 구간은 모델 선택에 사용하지 않았습니다.", "",
                  "| 모델 | 검증 MAE (%p) | 테스트 MAE (%p) | 테스트 RMSE (%p) |", "|---|---:|---:|---:|"]
        for name in assessment["validation_metrics"]:
            validation = assessment["validation_metrics"][name]
            test = assessment["test_metrics"][name]
            lines.append(f"| {name} | {validation['mae_percentage_points']:.3f} | {test['mae_percentage_points']:.3f} | {test['rmse_percentage_points']:.3f} |")
        lines += ["", f"선택: **{assessment['selected_model']}**. 테스트 MAE의 기준 예측 대비 개선율: **{assessment['test_improvement_pct']:.2f}%**.", "",
                  "음수 개선율은 기준 예측보다 나쁜 결과입니다. 기준 예측이 선택됐다면 학습 모델을 사용하는 근거가 아직 부족합니다.", "",
                  "![테스트 예측](backtest.png)", ""]
    stale = int(forecasts.stale_input.sum()) if len(forecasts) else 0
    lines += ["## 예측 및 재확인", "",
              "`next_predictions.csv`의 예측 시점은 다운로드에 포함된 마지막 수집 시각입니다. 현재 시각의 실시간 예측으로 해석하지 않습니다.",
              f"현재 생성 시점에 입력이 20분 이상 오래된 주차구역: {stale}개.", "",
              "다음 동기화 후 `evaluate` 명령으로 이 파일을 실제 관측과 비교하세요. 해당 목표 시점 자료가 아직 없으면 미확인으로 남습니다.", "",
              "## 분석 규칙", "",
              "- 혼잡률 = 주차 대수 / 총 주차면. 미운영(총 주차면 0)은 학습·예측에서 제외합니다.",
              "- 초과 점유 원값과 100%를 넘는 예측을 보존합니다.",
              "- 승객예고는 실제 승객 수가 아니며 예측 시점까지 수집된 버전만 입력으로 사용합니다.",
              "- 누락 구간을 미래값으로 보간하지 않습니다. 목표 시각 ±허용 오차 내 실제 관측이 있는 사례만 학습합니다.",
              "- 학습과 검증 사이에는 목표값 확보 시각을 기준으로 겹침을 제거합니다.",
              "- 혼잡 판정 90%는 이 실험의 초기 기준이며 공항의 공식 분류가 아닙니다.",
              "- 데이터가 7일 이상 있어도 계절·공휴일·장기간 변화를 검증한 것은 아닙니다.", ""]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def train(args):
    snapshot = prepare_dataset(args)
    parking, passengers, quality = load_snapshot(args.data_root, snapshot)
    records = build_features(parking, passengers, args.horizon_minutes)
    if records.empty:
        raise ValueError("No operating parking areas are available")
    tagged = attach_targets(records, parking, args.tolerance_minutes)
    dataset = tagged.dropna(subset=["actual_ratio"]).copy()
    assessment = {"status": "insufficient_data", "horizon_minutes": args.horizon_minutes,
                  "tolerance_minutes": args.tolerance_minutes, "labeled_rows": len(dataset),
                  "busy_threshold": args.busy_threshold, "reasons": [], "selected_model": "persistence"}
    if quality["span_days"] < args.min_span_days:
        assessment["reasons"].append(f"관측 기간이 {quality['span_days']:.2f}일로 최소 {args.min_span_days:g}일보다 짧습니다.")
    if dataset.empty:
        assessment["reasons"].append("지정한 예측 시점과 연결할 미래 실제값이 없습니다.")
    else:
        labeled_span = (dataset.origin_at.max() - dataset.origin_at.min()).total_seconds() / 86400
        labeled_days = dataset.origin_at.dt.tz_convert(TIMEZONE).dt.date.nunique()
        assessment.update(labeled_span_days=labeled_span, labeled_days=int(labeled_days))
        if labeled_span < args.min_span_days or labeled_days < np.ceil(args.min_span_days):
            assessment["reasons"].append(f"실제값과 연결된 학습 사례의 기간이 {labeled_span:.2f}일, 관측 날짜가 {labeled_days}개로 부족합니다.")
    splits = None
    if not dataset.empty:
        try:
            splits = temporal_split(dataset)
            sizes = {name: {"rows": len(frame), "prediction_times": int(frame.origin_at.nunique()),
                            "first_origin_at": frame.origin_at.min().isoformat() if len(frame) else None,
                            "last_origin_at": frame.origin_at.max().isoformat() if len(frame) else None}
                     for name, frame in zip(("train", "validation", "test"), splits)}
            assessment["splits"] = sizes
            for name, minimum in (("train", 48), ("validation", 12), ("test", 12)):
                if sizes[name]["prediction_times"] < minimum:
                    assessment["reasons"].append(f"{name}의 예측 시각이 {sizes[name]['prediction_times']}개로 최소 {minimum}개보다 적습니다.")
        except ValueError as exc:
            assessment["reasons"].append(str(exc))
    models = {"persistence": None}
    test_results = None
    if not assessment["reasons"]:
        training, validation, testing = splits
        for name in ("ridge", "random_forest"):
            models[name] = estimator(name).fit(training[FEATURES], training.actual_ratio)
        assessment["validation_metrics"] = {name: metrics(validation.actual_ratio, predict(model, validation), args.busy_threshold) for name, model in models.items()}
        selected = min(models, key=lambda name: assessment["validation_metrics"][name]["mae_percentage_points"])
        test_results = testing[["lot_name", "origin_at", "requested_target_at", "actual_observed_at", "label_available_at", "actual_ratio", "current_ratio"]].copy()
        assessment["test_metrics"] = {}
        for name, model in models.items():
            test_results[name] = predict(model, testing)
            assessment["test_metrics"][name] = metrics(testing.actual_ratio, test_results[name], args.busy_threshold)
        baseline_error = assessment["test_metrics"]["persistence"]["mae_percentage_points"]
        selected_error = assessment["test_metrics"][selected]["mae_percentage_points"]
        assessment["test_improvement_pct"] = (baseline_error - selected_error) / baseline_error * 100 if baseline_error > 0 else 0.0
        assessment.update(status="evaluated", selected_model=selected)
        # Test metrics refer to the training-only fits above; the deployment model uses all resolved history.
        if selected != "persistence":
            models[selected] = estimator(selected).fit(dataset[FEATURES], dataset.actual_ratio)
    selected = assessment["selected_model"]
    current_lots = parking.sort_values("collected_at").groupby("lot_name", sort=False).tail(1)
    operating_lots = current_lots.loc[current_lots.total_spaces > 0, "lot_name"]
    latest = records.loc[records.lot_name.isin(operating_lots)].sort_values("origin_at").groupby("lot_name", sort=False).tail(1).copy()
    latest["predicted_ratio"] = predict(models[selected], latest)
    latest["predicted_occupancy_pct"] = latest.predicted_ratio * 100
    latest["estimated_occupied_spaces"] = np.rint(latest.predicted_ratio * latest.total_spaces).astype(int)
    latest["predicted_busy"] = latest.predicted_ratio >= args.busy_threshold
    latest["model"] = selected
    latest["prediction_kind"] = "learned" if selected != "persistence" else "baseline"
    latest["stale_input"] = datetime.now(timezone.utc) - latest.origin_at > pd.Timedelta(minutes=20)
    forecasts = latest[["lot_name", "origin_at", "observed_at", "requested_target_at", "total_spaces", "current_ratio", "predicted_ratio", "predicted_occupancy_pct", "estimated_occupied_spaces", "predicted_busy", "model", "prediction_kind", "stale_input"]]
    folder = args.output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    quality["passenger_rows"] = len(passengers)
    quality["operating_rows"] = len(records)
    quality["forecast_feature_coverage_pct"] = float(records.passengers_target_arrival.notna().mean() * 100)
    for name, content in (("data_quality.json", quality), ("metrics.json", assessment)):
        (folder / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tagged.to_csv(folder / "training_examples.csv", index=False, encoding="utf-8-sig")
    forecasts.to_csv(folder / "next_predictions.csv", index=False, encoding="utf-8-sig")
    if test_results is not None:
        test_results.to_csv(folder / "test_predictions.csv", index=False, encoding="utf-8-sig")
        per_lot = []
        for lot, group in test_results.groupby("lot_name"):
            for name in models:
                per_lot.append({"lot_name": lot, "model": name, **metrics(group.actual_ratio, group[name], args.busy_threshold)})
        pd.DataFrame(per_lot).to_csv(folder / "per_lot_metrics.csv", index=False, encoding="utf-8-sig")
        backtest_plot(test_results, selected, folder / "backtest.png")
    bundle = {"estimator": models[selected], "selected_model": selected, "assessment_status": assessment["status"],
              "horizon_minutes": args.horizon_minutes, "features": FEATURES,
              "training_snapshot": quality["snapshot"], "trained_rows": len(dataset) if selected != "persistence" else 0,
              "fitted_through_origin_at": dataset.origin_at.max().isoformat() if selected != "persistence" else None}
    with (folder / "model.pkl").open("wb") as output:
        pickle.dump(bundle, output)
    coverage_plot(parking, folder / "coverage.png")
    write_report(folder, quality, assessment, forecasts)
    pointer = args.output_root / (".latest-" + uuid.uuid4().hex + ".json")
    pointer.write_text(json.dumps({"run_dir": folder.name, "status": assessment["status"]}, indent=2), encoding="utf-8")
    pointer.replace(args.output_root / "latest.json")
    print(json.dumps({"status": assessment["status"], "labeled_rows": len(dataset), "selected_model": selected, "report": str(folder / "report.md")}, ensure_ascii=True))
    return folder


def evaluate(args):
    snapshot = prepare_dataset(args)
    parking, _, quality = load_snapshot(args.data_root, snapshot)
    predictions = pd.read_csv(args.predictions)
    for column in ("origin_at", "requested_target_at"):
        predictions[column] = timestamps(predictions[column])
    predictions["predicted_ratio"] = pd.to_numeric(predictions.predicted_ratio, errors="raise")
    checked = attach_targets(predictions, parking, args.tolerance_minutes)
    checked["evaluation_status"] = np.where(checked.actual_ratio.notna(), "matched", "pending_or_missing_observation")
    valid = checked.dropna(subset=["actual_ratio"])
    summary = {"prediction_rows": len(checked), "matched_rows": len(valid), "pending_or_missing_rows": len(checked) - len(valid),
               "snapshot": quality["snapshot"], "tolerance_minutes": args.tolerance_minutes}
    if len(valid):
        summary["forecast_metrics"] = metrics(valid.actual_ratio, valid.predicted_ratio, args.busy_threshold)
        summary["baseline_metrics"] = metrics(valid.actual_ratio, valid.current_ratio, args.busy_threshold)
        checked["absolute_error_percentage_points"] = (checked.predicted_ratio - checked.actual_ratio).abs() * 100
    destination = args.predictions.with_name(args.predictions.stem + ".evaluation.csv")
    checked.to_csv(destination, index=False, encoding="utf-8-sig")
    destination.with_suffix(".json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True))
    return summary


def prepare_dataset(args):
    # Explicit historical snapshots remain reproducible and do not contact SSH.
    if args.snapshot is not None:
        return args.snapshot
    return ensure_fresh_dataset(args.data_root, getattr(args, "sync_config", None))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    training = commands.add_parser("train", help="Check data readiness, backtest models and generate forecasts")
    evaluation = commands.add_parser("evaluate", help="Compare previously saved forecasts with a later download")
    for subparser in (training, evaluation):
        subparser.add_argument("--data-root", type=Path, default=ROOT / "data/raw/server")
        subparser.add_argument("--snapshot", type=Path, help="Use a specific export instead of latest.json")
        subparser.add_argument("--sync-config", type=Path, help="SSH config for automatic refresh; defaults to local-work/server-sync.json")
        subparser.add_argument("--tolerance-minutes", type=int, default=10)
        subparser.add_argument("--busy-threshold", type=float, default=0.9)
    training.add_argument("--horizon-minutes", type=int, default=60)
    training.add_argument("--min-span-days", type=float, default=7)
    training.add_argument("--output-root", type=Path, default=ROOT / "data/processed/congestion")
    evaluation.add_argument("--predictions", required=True, type=Path)
    args = parser.parse_args()
    if args.tolerance_minutes <= 0 or not 0 < args.busy_threshold <= 1:
        parser.error("Tolerance must be positive and busy threshold must be in (0, 1]")
    if args.command == "train":
        if args.horizon_minutes <= args.tolerance_minutes or args.min_span_days < 0:
            parser.error("Horizon must exceed tolerance and minimum span must be nonnegative")
        train(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
