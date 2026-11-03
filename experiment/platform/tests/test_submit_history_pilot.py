import csv
import json

import pytest

from experiment.platform.tools.submit_history_pilot import build_payload


def _pilot_inputs(tmp_path, windows):
    ledger = tmp_path / "campaign" / "runs.csv"
    ledger.parent.mkdir()
    with ledger.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["run_id", "phase", "status"])
        writer.writeheader()
        for run_id in windows:
            writer.writerow({"run_id": run_id, "phase": "test", "status": "completed"})
            start, end = windows[run_id]
            run_dir = tmp_path / "objects" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "observation_window.json").write_text(
                json.dumps({"window": {"start": start, "end": end}}),
                encoding="utf-8",
            )
    return ledger, tmp_path / "objects"


def test_pilot_payload_orders_runs_by_window_start(tmp_path):
    ledger, object_store = _pilot_inputs(
        tmp_path,
        {
            "run-later": ("2026-10-28T12:00:00Z", "2026-10-28T12:30:00Z"),
            "run-earlier": ("2026-10-28T11:00:00Z", "2026-10-28T11:30:00Z"),
        },
    )

    payload = build_payload(ledger, object_store, "campaign", "test", "sock-shop")

    assert [run["run_id"] for run in payload["runs"]] == [
        "run-earlier",
        "run-later",
    ]


def test_pilot_payload_rejects_overlapping_run_windows(tmp_path):
    ledger, object_store = _pilot_inputs(
        tmp_path,
        {
            "run-earlier": ("2026-10-28T11:00:00Z", "2026-10-28T12:30:00Z"),
            "run-later": ("2026-10-28T12:00:00Z", "2026-10-28T13:00:00Z"),
        },
    )

    with pytest.raises(ValueError, match="run windows overlap"):
        build_payload(ledger, object_store, "campaign", "test", "sock-shop")
