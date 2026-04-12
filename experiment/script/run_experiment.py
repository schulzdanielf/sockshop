#!/usr/bin/env python3
"""Run load experiments with Locust and export Prometheus metrics to CSV.

What this script does per round:
1. Saves key timestamps (start, warmup start, ready, end)
2. Starts Locust with configurable users/spawn-rate
3. Waits for warmup + experiment duration
4. Pulls metrics from Prometheus using the recorded time window
5. Stores all samples with load_state labels in CSV
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shlex
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_METRICS = {
    "qps_total": 'sum(rate(request_duration_seconds_count{route!="metrics"}[1m]))',
    "qps_2xx": 'sum(rate(request_duration_seconds_count{route!="metrics",status_code=~"2.."}[1m]))',
    "qps_5xx": 'sum(rate(request_duration_seconds_count{route!="metrics",status_code=~"5.."}[1m]))',
    "frontend_p95": 'histogram_quantile(0.95, sum(rate(request_duration_seconds_bucket{name="front-end"}[1m])) by (le))',
}


def utc_iso_from_epoch(epoch_seconds: float) -> str:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc).isoformat()


def now_epoch() -> float:
    return time.time()


def read_metrics_config(metrics_file: Optional[str]) -> Dict[str, str]:
    if not metrics_file:
        return DEFAULT_METRICS.copy()

    file_path = Path(metrics_file)
    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict) or not payload:
        raise ValueError("Metrics config must be a non-empty JSON object: {name: promql}")

    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Each metrics entry must be string:string")
    return payload


def _http_get_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_vector_or_scalar(payload: dict) -> Optional[float]:
    if payload.get("status") != "success":
        return None

    data = payload.get("data", {})
    result_type = data.get("resultType")
    result = data.get("result")

    if result_type == "vector":
        if not result:
            return None
        values: List[float] = []
        for item in result:
            value_pair = item.get("value", [None, None])
            try:
                values.append(float(value_pair[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return sum(values) if values else None

    if result_type == "scalar":
        try:
            return float(result[1])
        except (TypeError, ValueError, IndexError):
            return None

    return None


def query_prometheus_at(prom_url: str, promql: str, when_epoch: float, timeout: int) -> Optional[float]:
    endpoint = prom_url.rstrip("/") + "/api/v1/query"
    query = urllib.parse.urlencode({"query": promql, "time": f"{when_epoch:.3f}"})
    url = endpoint + "?" + query
    try:
        payload = _http_get_json(url, timeout=timeout)
        return _parse_vector_or_scalar(payload)
    except Exception:
        return None


def _sum_sample_values(sample: list) -> Optional[float]:
    total = 0.0
    found = False
    for item in sample:
        try:
            total += float(item[1])
            found = True
        except (TypeError, ValueError, IndexError):
            continue
    return total if found else None


def query_prometheus_range(
    prom_url: str,
    promql: str,
    start_epoch: float,
    end_epoch: float,
    step_seconds: int,
    timeout: int,
) -> Dict[int, Optional[float]]:
    endpoint = prom_url.rstrip("/") + "/api/v1/query_range"
    params = {
        "query": promql,
        "start": f"{start_epoch:.3f}",
        "end": f"{end_epoch:.3f}",
        "step": str(step_seconds),
    }
    url = endpoint + "?" + urllib.parse.urlencode(params)

    try:
        payload = _http_get_json(url, timeout=timeout)
    except Exception:
        return {}

    if payload.get("status") != "success":
        return {}

    result_type = payload.get("data", {}).get("resultType")
    result = payload.get("data", {}).get("result", [])
    if result_type != "matrix" or not isinstance(result, list):
        return {}

    merged: Dict[int, float] = {}
    seen: Dict[int, int] = {}

    for series in result:
        values = series.get("values", [])
        for item in values:
            try:
                ts = int(float(item[0]))
                val = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            merged[ts] = merged.get(ts, 0.0) + val
            seen[ts] = seen.get(ts, 0) + 1

    out: Dict[int, Optional[float]] = {}
    for ts in range(int(start_epoch), int(end_epoch) + 1, step_seconds):
        out[ts] = merged.get(ts)
    return out


def run_locust_round(args: argparse.Namespace, run_total_seconds: int) -> subprocess.CompletedProcess[str]:
    cmd = shlex.split(args.locust_cmd) + [
        "-f",
        args.locust_file,
        "--host",
        args.locust_host,
        "--headless",
        "--users",
        str(args.users),
        "--spawn-rate",
        str(args.spawn_rate),
        "--run-time",
        f"{run_total_seconds}s",
    ]

    if args.locust_extra_args:
        cmd.extend(args.locust_extra_args.split())

    try:
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=run_total_seconds + args.locust_grace_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Comando do Locust nao encontrado. "
            "Use um binario local (locust) ou execute no container com --locust-cmd 'kubectl -n loadtest exec deploy/locust-web -- locust'."
        ) from exc


def state_for_timestamp(
    ts: int,
    disabled_end: float,
    warmup_end: float,
    run_end: float,
) -> str:
    if ts < int(disabled_end):
        return "DESATIVADA"
    if ts < int(warmup_end):
        return "AQUECIMENTO"
    if ts <= int(run_end):
        return "EXECUCAO"
    return "DESATIVADA"


def run(args: argparse.Namespace) -> Path:
    metrics = read_metrics_config(args.metrics_file)
    metric_names = list(metrics.keys())

    rows: List[Dict[str, object]] = []

    for round_number in range(1, args.rounds + 1):
        round_start = now_epoch()

        if args.disabled_seconds > 0:
            time.sleep(args.disabled_seconds)

        warmup_start = now_epoch()
        warmup_end = warmup_start + args.warmup_seconds
        run_end = warmup_end + args.run_seconds
        locust_total = args.warmup_seconds + args.run_seconds

        locust_result = run_locust_round(args=args, run_total_seconds=locust_total)

        round_end = now_epoch()
        # If locust exits earlier/later for any reason, keep a deterministic data window.
        data_end = min(max(run_end, warmup_start), round_end)

        by_metric: Dict[str, Dict[int, Optional[float]]] = {}
        for metric_name, promql in metrics.items():
            by_metric[metric_name] = query_prometheus_range(
                prom_url=args.prom_url,
                promql=promql,
                start_epoch=round_start,
                end_epoch=data_end,
                step_seconds=args.sample_interval_seconds,
                timeout=args.prom_timeout_seconds,
            )

        ts_points = list(range(int(round_start), int(data_end) + 1, args.sample_interval_seconds))
        for ts in ts_points:
            row: Dict[str, object] = {
                "timestamp_utc": utc_iso_from_epoch(ts),
                "round": round_number,
                "load_state": state_for_timestamp(
                    ts=ts,
                    disabled_end=warmup_start,
                    warmup_end=warmup_end,
                    run_end=run_end,
                ),
                "round_start_utc": utc_iso_from_epoch(round_start),
                "warmup_start_utc": utc_iso_from_epoch(warmup_start),
                "ready_utc": utc_iso_from_epoch(warmup_end),
                "round_end_utc": utc_iso_from_epoch(data_end),
                "locust_exit_code": locust_result.returncode,
            }
            for metric_name in metric_names:
                row[metric_name] = by_metric.get(metric_name, {}).get(ts)
            rows.append(row)

        ready_row: Dict[str, object] = {
            "timestamp_utc": utc_iso_from_epoch(warmup_end),
            "round": round_number,
            "load_state": "PRONTA",
            "round_start_utc": utc_iso_from_epoch(round_start),
            "warmup_start_utc": utc_iso_from_epoch(warmup_start),
            "ready_utc": utc_iso_from_epoch(warmup_end),
            "round_end_utc": utc_iso_from_epoch(data_end),
            "locust_exit_code": locust_result.returncode,
        }
        for metric_name, promql in metrics.items():
            ready_row[metric_name] = query_prometheus_at(
                prom_url=args.prom_url,
                promql=promql,
                when_epoch=warmup_end,
                timeout=args.prom_timeout_seconds,
            )
        rows.append(ready_row)

        if args.fail_on_locust_error and locust_result.returncode != 0:
            raise RuntimeError(
                "Locust failed in round "
                f"{round_number} with exit code {locust_result.returncode}\n"
                f"stdout:\n{locust_result.stdout}\n"
                f"stderr:\n{locust_result.stderr}"
            )

    out_path = Path(args.output_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    headers = [
        "timestamp_utc",
        "round",
        "load_state",
        "round_start_utc",
        "warmup_start_utc",
        "ready_utc",
        "round_end_utc",
        "locust_exit_code",
        *metric_names,
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run multi-round locust experiments and export Prometheus metrics to CSV"
    )
    parser.add_argument("--prom-url", default="http://localhost:9090", help="Prometheus base URL")
    parser.add_argument("--prom-timeout-seconds", type=int, default=10, help="Prometheus request timeout")
    parser.add_argument("--metrics-file", default=None, help="JSON map: metric_name -> promql")
    parser.add_argument("--output-csv", default="experiment/data/experiment.csv", help="Output CSV path")

    parser.add_argument("--rounds", type=int, default=3, help="Number of rounds")
    parser.add_argument("--disabled-seconds", type=int, default=0, help="Idle time before warmup")
    parser.add_argument("--warmup-seconds", type=int, default=60, help="Warmup duration")
    parser.add_argument("--run-seconds", type=int, default=300, help="Experiment duration after warmup")
    parser.add_argument("--sample-interval-seconds", type=int, default=15, help="Prometheus range step")

    parser.add_argument(
        "--locust-cmd",
        default="locust",
        help="Comando para executar o Locust. Aceita comando completo, ex: 'kubectl -n loadtest exec deploy/locust-web -- locust'",
    )
    parser.add_argument("--locust-file", required=True, help="Path to locustfile.py")
    parser.add_argument("--locust-host", required=True, help="Target host for load")
    parser.add_argument("--users", type=int, default=20, help="Concurrent users")
    parser.add_argument("--spawn-rate", type=float, default=5.0, help="User growth rate")
    parser.add_argument("--locust-extra-args", default="", help="Extra locust CLI args")
    parser.add_argument("--locust-grace-seconds", type=int, default=30, help="Extra timeout buffer")
    parser.add_argument(
        "--fail-on-locust-error",
        action="store_true",
        help="Stop script if locust exits with non-zero code",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    output = run(args)
    print(f"CSV gerado em: {output}")


if __name__ == "__main__":
    main()
