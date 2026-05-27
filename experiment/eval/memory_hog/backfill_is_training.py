"""Backfill ``run_features.is_training`` from the harness ``runs.csv``.

After adding the ``is_training`` column to ``run_features`` with default 1,
existing rows are all marked as training corpus. This is incorrect for the
held-out test rows produced by the eval harness — they must be flipped to 0
so they don't leak as RAG neighbours when the test cells are re-evaluated.

Usage::

    .venv/bin/python -m experiment.eval.memory_hog.backfill_is_training \
        --runs-csv experiment/eval/memory_hog/out/runs.csv \
        --db experiment/platform/data/platform.db
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


def backfill(runs_csv: Path, db: Path, *, dry_run: bool = False) -> int:
    if not runs_csv.exists():
        print(f"runs.csv not found: {runs_csv}", file=sys.stderr)
        return 2
    if not db.exists():
        print(f"sqlite db not found: {db}", file=sys.stderr)
        return 2

    updates: list[tuple[int, str]] = []  # (is_training, run_id)
    with runs_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            run_id = (row.get("run_id") or "").strip()
            phase = (row.get("phase") or "").strip().lower()
            if not run_id or not phase:
                continue
            updates.append((1 if phase == "train" else 0, run_id))

    if not updates:
        print("no rows to update", file=sys.stderr)
        return 1

    train = sum(1 for v, _ in updates if v == 1)
    test = len(updates) - train
    print(f"Planned updates: {len(updates)} (train={train}, test={test})")

    if dry_run:
        for v, rid in updates[:5]:
            print(f"  would set is_training={v} on {rid}")
        if len(updates) > 5:
            print(f"  ... and {len(updates) - 5} more")
        return 0

    conn = sqlite3.connect(str(db))
    try:
        cur = conn.cursor()
        applied = 0
        for is_training, run_id in updates:
            cur.execute(
                "UPDATE run_features SET is_training = ? WHERE run_id = ?",
                (is_training, run_id),
            )
            applied += cur.rowcount
        conn.commit()
    finally:
        conn.close()

    print(f"Applied {applied} updates ({len(updates) - applied} run_ids not in DB).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--runs-csv",
        type=Path,
        default=Path("experiment/eval/memory_hog/out/runs.csv"),
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=Path("experiment/platform/data/platform.db"),
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return backfill(args.runs_csv, args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
