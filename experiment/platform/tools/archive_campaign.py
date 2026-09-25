#!/usr/bin/env python3
"""Archive replayable evidence for one experiment campaign."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OBJECT_STORE = ROOT / "experiment" / "platform" / "data" / "object_store"


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_rows(ledger: Path) -> list[dict[str, str]]:
    with ledger.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def archive(ledger: Path, object_store: Path, output: Path, dry_run: bool) -> int:
    rows = _load_rows(ledger)
    run_ids = sorted({row["run_id"] for row in rows if row.get("run_id")})
    required_ids = {
        row["run_id"]
        for row in rows
        if row.get("run_id") and row.get("status") == "completed"
    }
    missing = sorted(
        run_id for run_id in required_ids if not (object_store / run_id).is_dir()
    )
    files = [ledger]
    files.extend(
        path
        for run_id in run_ids
        if (object_store / run_id).is_dir()
        for path in sorted((object_store / run_id).rglob("*"))
        if path.is_file()
    )
    if dry_run:
        print(json.dumps({"runs": len(run_ids), "files": len(files), "missing": missing}, indent=2))
        return 0 if not missing else 2

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix("")
    staging.mkdir(parents=True, exist_ok=True)
    ledger_copy = staging / "runs.csv"
    ledger_copy.write_bytes(ledger.read_bytes())
    objects = staging / "objects"
    for run_id in run_ids:
        source = object_store / run_id
        if source.is_dir():
            destination = objects / run_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, dirs_exist_ok=True)

    archived_files = [ledger_copy, *sorted(p for p in objects.rglob("*") if p.is_file())]
    checksums = "".join(f"{_sha256(path)}  {path.relative_to(staging)}\n" for path in archived_files)
    (staging / "checksums.sha256").write_text(checksums, encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_revision": _git_revision(),
        "ledger": str(ledger),
        "object_store": str(object_store),
        "runs": len(run_ids),
        "statuses": dict(Counter(row.get("status", "unknown") for row in rows)),
        "missing_object_store_runs": missing,
        "files": len(archived_files),
        "replay_sources": ["runs.csv", "objects/<run_id>/*.json"],
        "live_backends_not_archived": ["prometheus", "tempo", "loki"],
    }
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with tarfile.open(output, "w:gz") as archive_file:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive_file.add(path, arcname=path.relative_to(staging))
    print(json.dumps(manifest, indent=2))
    return 0 if not missing else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--object-store", type=Path, default=DEFAULT_OBJECT_STORE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return archive(args.ledger, args.object_store, args.output, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
