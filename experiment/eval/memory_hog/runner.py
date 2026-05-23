#!/usr/bin/env python3
"""End-to-end harness for the memory-hog evaluation experiment.

Phases:
  1. **TRAIN** — inject memory-hog in each `train_services × replicas` cell;
     the platform engine writes features/summary/embedding into the RAG corpus
     automatically.
  2. **TEST**  — same loop on `test_services × replicas`; record the run_id
     and the known ``chaos_target`` (the service we injected into).
  3. **EVAL**  — for every test run × every strategy in `strategies`, call
     ``POST /api/runs/{id}/llm-analysis?force=true&...`` and capture the
     predicted RCA. Result rows go to ``out/evaluation.csv``.

Designed to run unattended end-to-end::

    python -m experiment.eval.memory_hog.runner            # full run
    python -m experiment.eval.memory_hog.runner --phase eval  # re-eval only
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ── Module-level constants ──────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CONFIG_PATH = HERE / "config.yaml"

TERMINAL_STATUSES = {"completed", "failed"}


# ── Tiny HTTP helper (no `requests` dep needed) ────────────────────────────
@dataclass
class ApiClient:
    base_url: str
    role: str = "operator"

    def _req(self, method: str, path: str, body: Optional[dict] = None,
             timeout: int = 60) -> Any:
        url = self.base_url.rstrip("/") + path
        data = None
        headers = {"X-User-Role": self.role, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP {exc.code} on {method} {path}: {body_text}"
            ) from exc
        if not raw:
            return None
        return json.loads(raw)

    def get(self, path: str, **q: Any) -> Any:
        if q:
            path += "?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        return self._req("GET", path)

    def post(self, path: str, body: Optional[dict] = None, **q: Any) -> Any:
        if q:
            path += "?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        return self._req("POST", path, body=body, timeout=600)


# ── Spec assembly (mirrors the frontend's buildSpec) ───────────────────────
def build_spec(cfg: Dict[str, Any], service: str, replica_idx: int) -> Dict[str, Any]:
    """Compose an experiment.v1 spec for one (service, replica) cell."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    exp_id = f"memhog-{service}-r{replica_idx}-{stamp}"
    chaos_engine = f"{service}-memory-hog"  # Argo workflow template name
    return {
        "schema_version": "1.0.0",
        "experiment": {
            "id": exp_id,
            "name": f"memory-hog · {service} · r{replica_idx}",
            "description_manual": (
                f"Pod memory-hog injection on {service} in {cfg['cluster']['app_namespace']}. "
                f"Replica {replica_idx} of automated memory-hog evaluation harness."
            ),
            "hypothesis": (
                f"Memory exhaustion on {service} causes container restart and "
                "transient latency/error spikes on the affected dependency chain."
            ),
            "tags": [
                "chaos", "platform-v1", "memory-hog",
                f"target:{service}", f"replica:{replica_idx}",
                "harness:memory-hog-eval",
            ],
        },
        "scope": {
            "environment": "staging",
            "namespace": cfg["cluster"]["app_namespace"],
        },
        "timeline": dict(cfg["timeline"]),
        "load_profile": {
            **cfg["load_profile"],
            "extra_args": [],
        },
        "chaos_profile": {
            "provider": "litmus",
            "namespace": cfg["cluster"]["app_namespace"],
            "manifest_path": None,
            "chaos_engine": chaos_engine,  # Argo workflow template (must exist)
            "chaos_result": None,
        },
        "observability": {
            "metrics": {
                "provider": "mcp-prometheus",
                "mcp_sse_url": "http://127.0.0.1:18080/sse",
                "timeout_seconds": 10,
                "queries": [
                    {"id": "traffic", "query": "sum(rate(request_duration_seconds_count[5m])) by (name)"},
                    {"id": "error_rate", "query": "sum(rate(request_duration_seconds_count{status_code=~\"5..\"}[5m])) by (name)"},
                    {"id": "latency_p95", "query": "histogram_quantile(0.95, sum(rate(request_duration_seconds_bucket[5m])) by (le,name))"},
                ],
            },
            "traces": {
                "provider": "mcp-tempo",
                "mcp_sse_url": "http://127.0.0.1:18080/sse",
                "timeout_seconds": 20,
                "query": "{ status = error }",
                "service_name": service,
                "limit": 10,
                "use_llm": False,
                "max_new_tokens": 256,
            },
        },
        "analysis": {
            "slo": dict(cfg["slo"]),
            "classification": {
                "resilient_rule": "no_slo_violation_or_recovery<=120s",
                "degraded_recoverable_rule": "slo_violation_and_recovery<=600s",
                "degraded_persistent_rule": "recovery>600s_or_not_recovered",
            },
        },
        "governance": {
            "requires_approval": False,
            "risk_level": "low",
        },
    }


# ── Run lifecycle helpers ──────────────────────────────────────────────────
@dataclass
class RunRecord:
    service: str
    replica_idx: int
    phase: str               # "train" or "test"
    chaos_target: str
    experiment_id: str
    run_id: Optional[str] = None
    status: str = "pending"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    notes: str = ""


def create_and_start_run(api: ApiClient, cfg: Dict[str, Any],
                         service: str, replica_idx: int, phase: str) -> RunRecord:
    spec = build_spec(cfg, service, replica_idx)
    rec = RunRecord(
        service=service, replica_idx=replica_idx, phase=phase,
        chaos_target=service, experiment_id=spec["experiment"]["id"],
    )
    # 1. Create experiment (returns id + version)
    api.post("/api/experiments", body={
        "initiated_by": cfg["platform"]["initiated_by"],
        "spec": spec,
    })
    # 2. Start run
    resp = api.post("/api/runs/start", body={
        "experiment_id": spec["experiment"]["id"],
        "initiated_by": cfg["platform"]["initiated_by"],
        "idempotency_key": f"{spec['experiment']['id']}-{uuid.uuid4().hex[:8]}",
    })
    rec.run_id = resp["run_id"]
    rec.status = resp["status"]
    return rec


def wait_for_run(api: ApiClient, run_id: str, *, poll_s: int, timeout_s: int) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = api.get(f"/api/runs/{run_id}")
        if last.get("status") in TERMINAL_STATUSES:
            return last
        time.sleep(poll_s)
    raise TimeoutError(f"Run {run_id} did not reach a terminal state within {timeout_s}s")


# ── Evaluation ─────────────────────────────────────────────────────────────
@dataclass
class StrategyResult:
    run_id: str
    chaos_target: str
    strategy: str
    predicted_rca: Optional[str]
    correct: Optional[bool]
    confidence: Optional[float]
    fault_category: Optional[str]
    prompt_tokens: Optional[int]
    neighbours_used: Optional[int]
    rag_mode: Optional[str]
    system_card_id: Optional[str]
    raw_error: str = ""


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def evaluate_strategy(api: ApiClient, run: RunRecord,
                      strategy: Dict[str, Any]) -> StrategyResult:
    name = strategy["name"]
    try:
        resp = api.post(
            f"/api/runs/{run.run_id}/llm-analysis",
            force="true",
            limit=strategy["limit"],
            mode=strategy["mode"],
            system_id=strategy["system_id"],
            budget_tokens=strategy["budget_tokens"],
        )
    except Exception as exc:  # pragma: no cover - network/LLM failures
        return StrategyResult(
            run_id=run.run_id or "",
            chaos_target=run.chaos_target,
            strategy=name,
            predicted_rca=None, correct=None, confidence=None,
            fault_category=None, prompt_tokens=None, neighbours_used=None,
            rag_mode=None, system_card_id=None,
            raw_error=str(exc)[:500],
        )

    analysis = resp.get("analysis", {}) if isinstance(resp, dict) else {}
    predicted = _safe_get(analysis, "rca")
    return StrategyResult(
        run_id=run.run_id or "",
        chaos_target=run.chaos_target,
        strategy=name,
        predicted_rca=predicted,
        correct=(predicted == run.chaos_target) if predicted else False,
        confidence=_safe_get(analysis, "confidence"),
        fault_category=_safe_get(analysis, "fault_category"),
        prompt_tokens=_safe_get(analysis, "prompt_meta", "tokens_estimate"),
        neighbours_used=_safe_get(analysis, "prompt_meta", "neighbours_used"),
        rag_mode=_safe_get(analysis, "rag_mode"),
        system_card_id=_safe_get(analysis, "prompt_meta", "system_card_id"),
    )


# ── CSV writers ─────────────────────────────────────────────────────────────
RUN_FIELDS = [
    "phase", "service", "replica_idx", "chaos_target",
    "experiment_id", "run_id", "status", "started_at", "ended_at", "notes",
]
EVAL_FIELDS = [
    "run_id", "chaos_target", "strategy", "predicted_rca", "correct",
    "confidence", "fault_category", "prompt_tokens", "neighbours_used",
    "rag_mode", "system_card_id", "raw_error",
]


def _write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def _load_runs_csv(path: Path) -> List[RunRecord]:
    if not path.exists():
        return []
    out: List[RunRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(RunRecord(
                service=r["service"], replica_idx=int(r["replica_idx"]),
                phase=r["phase"], chaos_target=r["chaos_target"],
                experiment_id=r["experiment_id"],
                run_id=r.get("run_id") or None, status=r.get("status") or "unknown",
                started_at=r.get("started_at") or None,
                ended_at=r.get("ended_at") or None,
                notes=r.get("notes") or "",
            ))
    return out


# ── Main orchestration ─────────────────────────────────────────────────────
def _flush_runs(runs_csv: Path, records: List[RunRecord]) -> None:
    """Persist runs.csv after every cell so a Ctrl-C doesn't lose progress."""
    _write_csv(runs_csv, [r.__dict__ for r in records], RUN_FIELDS)


def _run_chaos_loop(
    api: ApiClient,
    cfg: Dict[str, Any],
    services: List[str],
    replicas: int,
    phase: str,
    log_prefix: str,
    completed: Dict[tuple, RunRecord],
    all_records: List[RunRecord],
    runs_csv: Path,
) -> None:
    """Run chaos for every (service, replica) cell, appending into *all_records*.

    Cells whose key is present in *completed* (i.e. already 'completed' in a
    prior runs.csv) are skipped and the prior record is kept verbatim.
    Progress is flushed to *runs_csv* after every cell.
    """
    poll_s = int(cfg["platform"]["poll_interval_seconds"])
    timeout_s = int(cfg["platform"]["run_timeout_seconds"])
    for service in services:
        for rep in range(replicas):
            key = (phase, service, rep)
            prior = completed.get(key)
            if prior is not None:
                print(f"  [{log_prefix}] skip   service={service} replica={rep} "
                      f"(already completed run_id={prior.run_id})")
                all_records.append(prior)
                _flush_runs(runs_csv, all_records)
                continue
            print(f"  [{log_prefix}] start  service={service} replica={rep}")
            try:
                rec = create_and_start_run(api, cfg, service, rep, phase)
            except Exception as exc:
                rec = RunRecord(
                    service=service, replica_idx=rep, phase=phase,
                    chaos_target=service, experiment_id="",
                    notes=f"start_failed: {exc}"[:500],
                )
                all_records.append(rec)
                _flush_runs(runs_csv, all_records)
                print(f"  [{log_prefix}] FAIL   service={service} replica={rep}: {exc}")
                continue
            try:
                final = wait_for_run(api, rec.run_id, poll_s=poll_s, timeout_s=timeout_s)
                rec.status = final.get("status", rec.status)
                rec.started_at = final.get("started_at")
                rec.ended_at = final.get("ended_at")
                print(f"  [{log_prefix}] done   run_id={rec.run_id} status={rec.status}")
            except Exception as exc:
                rec.notes = f"wait_failed: {exc}"[:500]
                print(f"  [{log_prefix}] TIMEOUT run_id={rec.run_id}: {exc}")
            all_records.append(rec)
            _flush_runs(runs_csv, all_records)


def phase_chaos(
    api: ApiClient,
    cfg: Dict[str, Any],
    runs_csv: Path,
    retry_failed: bool = False,
) -> List[RunRecord]:
    """Run TRAIN+TEST chaos loops.

    If *retry_failed* is True and runs.csv exists, cells whose previous status
    is 'completed' are kept as-is; everything else is re-attempted.
    """
    completed: Dict[tuple, RunRecord] = {}
    if retry_failed:
        prior = _load_runs_csv(runs_csv)
        for rec in prior:
            if rec.status == "completed" and rec.run_id:
                completed[(rec.phase, rec.service, rec.replica_idx)] = rec
        print(f"[retry-failed] keeping {len(completed)} completed cell(s) "
              f"from previous run")

    all_records: List[RunRecord] = []
    print("--- PHASE 1: TRAIN (RAG corpus seeding) ---")
    _run_chaos_loop(
        api, cfg, cfg["train_services"], int(cfg["replicas"]),
        "train", "TRAIN", completed, all_records, runs_csv,
    )
    print("--- PHASE 2: TEST (held-out runs) ---")
    _run_chaos_loop(
        api, cfg, cfg["test_services"], int(cfg["replicas"]),
        "test", "TEST ", completed, all_records, runs_csv,
    )
    print(f"Wrote {len(all_records)} run records to {runs_csv.relative_to(ROOT)}")
    return all_records


def phase_eval(api: ApiClient, cfg: Dict[str, Any], runs: List[RunRecord],
               eval_csv: Path) -> List[StrategyResult]:
    print("─── PHASE 3: EVAL (strategy sweep on test runs) ─────────────────")
    test_runs = [r for r in runs if r.phase == "test"
                 and r.run_id and r.status == "completed"]
    if not test_runs:
        print("No completed test runs to evaluate.")
        return []

    results: List[StrategyResult] = []
    for run in test_runs:
        for strategy in cfg["strategies"]:
            print(f"  [EVAL ] run={run.run_id} target={run.chaos_target} "
                  f"strategy={strategy['name']}")
            res = evaluate_strategy(api, run, strategy)
            results.append(res)
            if res.raw_error:
                print(f"         error: {res.raw_error}")
            else:
                print(f"         predicted={res.predicted_rca} "
                      f"correct={res.correct} "
                      f"confidence={res.confidence} "
                      f"tokens={res.prompt_tokens}")
    _write_csv(eval_csv, [r.__dict__ for r in results], EVAL_FIELDS)
    print(f"Wrote {len(results)} evaluation rows to {eval_csv.relative_to(ROOT)}")
    _print_summary(results, cfg)
    return results


def _print_summary(results: List[StrategyResult], cfg: Dict[str, Any]) -> None:
    print()
    print("─── Summary: top-1 RCA accuracy per strategy ────────────────────")
    print(f"{'strategy':<32} {'n':>4} {'acc':>8} {'avg_tok':>10}")
    grouped: Dict[str, List[StrategyResult]] = {}
    for r in results:
        grouped.setdefault(r.strategy, []).append(r)
    for strat in cfg["strategies"]:
        name = strat["name"]
        rows = grouped.get(name, [])
        if not rows:
            continue
        valid = [r for r in rows if r.predicted_rca is not None]
        n = len(rows)
        acc = (sum(1 for r in valid if r.correct) / n) if n else 0.0
        avg_tok = (
            sum((r.prompt_tokens or 0) for r in valid) / len(valid)
        ) if valid else 0.0
        print(f"{name:<32} {n:>4} {acc:>8.2%} {avg_tok:>10.0f}")


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument(
        "--phase", choices=["all", "chaos", "eval"], default="all",
        help="Phase to run. 'eval' reuses runs.csv from a previous chaos phase.",
    )
    p.add_argument(
        "--retry-failed", action="store_true",
        help="Re-run only chaos cells that are not 'completed' in runs.csv; "
             "completed cells are kept verbatim.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Print the spec for the first cell and exit.")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    out_root = Path(cfg["output"]["results_dir"])
    if not out_root.is_absolute():
        out_root = ROOT / out_root
    runs_csv = out_root / cfg["output"]["runs_csv"]
    eval_csv = out_root / cfg["output"]["evaluation_csv"]

    api = ApiClient(
        base_url=cfg["platform"]["api_base"],
        role=cfg["platform"]["user_role"],
    )

    if args.dry_run:
        svc = cfg["train_services"][0]
        spec = build_spec(cfg, svc, 0)
        print(json.dumps(spec, indent=2))
        return 0

    # Health check first — fail fast if the platform isn't running.
    try:
        api.get("/api/system-cards")
    except Exception as exc:
        print(f"Cannot reach platform at {cfg['platform']['api_base']}: {exc}",
              file=sys.stderr)
        return 2

    runs: List[RunRecord] = []
    if args.phase in ("all", "chaos"):
        runs = phase_chaos(api, cfg, runs_csv, retry_failed=args.retry_failed)
    else:
        runs = _load_runs_csv(runs_csv)
        if not runs:
            print(f"--phase eval needs an existing {runs_csv}; run --phase chaos first",
                  file=sys.stderr)
            return 2

    if args.phase in ("all", "eval"):
        phase_eval(api, cfg, runs, eval_csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
