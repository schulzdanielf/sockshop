#!/usr/bin/env python3
"""Build or submit the initial test-run telemetry migration Job."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from experiment.platform.backend.history_job import HistoryMigrationDispatcher


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OBJECT_STORE = ROOT / "experiment" / "platform" / "data" / "object_store"


def _window_bounds(run: Dict[str, Any]) -> tuple[datetime, datetime] | None:
    observation_window = run.get("observation_window")
    bounds = (
        observation_window.get("window")
        if isinstance(observation_window, dict)
        else None
    )
    if not isinstance(bounds, dict) or not bounds.get("start") or not bounds.get("end"):
        return None
    start = datetime.fromisoformat(str(bounds["start"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(bounds["end"]).replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError(f"{run.get('run_id', 'run')}: invalid observation window")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def build_payload(
    ledger: Path,
    object_store: Path,
    campaign_id: str,
    phase: str,
    namespace: str,
) -> Dict[str, Any]:
    with ledger.open(newline="", encoding="utf-8") as stream:
        rows = [row for row in csv.DictReader(stream) if row.get("phase") == phase]
    if not rows:
        raise ValueError(f"ledger has no runs in phase {phase!r}")

    runs = []
    for row in rows:
        run_id = row.get("run_id", "")
        if not run_id:
            continue
        run_dir = object_store / run_id
        window_path = run_dir / "observation_window.json"
        log_path = run_dir / "rca_evidence_snapshot.json"
        observation_window = (
            json.loads(window_path.read_text(encoding="utf-8"))
            if window_path.is_file()
            else None
        )
        log_snapshot = (
            json.loads(log_path.read_text(encoding="utf-8"))
            if log_path.is_file()
            else None
        )
        trace_path = run_dir / "traces_raw.json"
        trace_collection = None
        if trace_path.is_file():
            trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
            trace_collection = trace_data.get("collection")
        runs.append(
            {
                "run_id": run_id,
                "status": row.get("status", "unknown"),
                "namespace": namespace,
                "observation_window": observation_window,
                "log_snapshot": log_snapshot,
                "trace_collection": trace_collection,
            }
        )
    if len(runs) != len(rows):
        raise ValueError("some selected ledger rows do not contain a run_id")
    runs_with_bounds = [(run, _window_bounds(run)) for run in runs]
    runs_with_bounds.sort(
        key=lambda item: (
            item[1] is None,
            item[1][0] if item[1] else datetime.max.replace(tzinfo=timezone.utc),
            item[0]["run_id"],
        )
    )
    bounded_runs = [
        (run, bounds) for run, bounds in runs_with_bounds if bounds is not None
    ]
    for (previous, previous_bounds), (current, current_bounds) in zip(
        bounded_runs, bounded_runs[1:]
    ):
        if previous_bounds[1] > current_bounds[0]:
            raise ValueError(
                "run windows overlap; chronological Prometheus migration "
                f"cannot order {previous['run_id']} before {current['run_id']}"
            )
    runs = [run for run, _ in runs_with_bounds]
    return {"campaign_id": campaign_id, "phase": phase, "runs": runs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--object-store", type=Path, default=DEFAULT_OBJECT_STORE)
    parser.add_argument("--campaign-id")
    parser.add_argument("--phase", default="test")
    parser.add_argument("--namespace", default="sock-shop")
    parser.add_argument("--history-namespace", default="monitoring-history")
    parser.add_argument("--image", default="sockshop/telemetry-history-migrator:local")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()

    campaign_id = args.campaign_id or args.ledger.parent.name
    payload = build_payload(
        args.ledger, args.object_store, campaign_id, args.phase, args.namespace
    )
    evidence_bytes = sum(
        len(json.dumps(run.get("log_snapshot") or {}, separators=(",", ":")))
        for run in payload["runs"]
    )
    summary = {
        "campaign_id": campaign_id,
        "phase": args.phase,
        "run_count": len(payload["runs"]),
        "statuses": {},
        "log_snapshot_bytes": evidence_bytes,
        "runs_with_window": sum(
            bool(run.get("observation_window")) for run in payload["runs"]
        ),
        "runs_with_log_snapshot": sum(
            bool(run.get("log_snapshot")) for run in payload["runs"]
        ),
    }
    for run in payload["runs"]:
        status = run["status"]
        summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
    if args.submit:
        dispatcher = HistoryMigrationDispatcher(
            enabled=True,
            namespace=args.history_namespace,
            image=args.image,
        )
        summary.update(dispatcher.submit_batch(payload))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"history pilot submission failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
