#!/usr/bin/env python3
"""End-to-end harness for the multi-chaos RCA evaluation experiment.

Phases:
  1. **TRAIN** — inject every (service, chaos_type) cell declared in
     ``train_experiments`` (× replicas); the platform engine writes
     features/summary/embedding into the RAG corpus automatically.
  2. **TEST**  — same loop on ``test_experiments``; record the run_id, the
     known ``chaos_target`` (the service we injected into) and the injected
     ``chaos_type``.
  3. **EVAL**  — for every test run × every strategy in `strategies`, call
     ``POST /api/runs/{id}/llm-analysis?force=true&...`` and capture the
     predicted RCA. Result rows go to ``out/evaluation.csv``.

Seven chaos types are wired into the default matrix: ``memory-hog``,
``cpu-hog``, ``pod-delete``, ``network-latency``, ``network-loss``,
``http-status-code`` and ``container-kill``. Train and test phases purposely
mix all of them. ``io-stress`` and ``dns-error`` baselines exist too but are
left out of the matrix because they do not run on docker-desktop / WSL2
(stress-ng O_DIRECT and dns_interceptor are unsupported there).

Network faults need the host ``sch_netem`` qdisc loaded first::

    make chaos-enable-netem    # sudo modprobe sch_netem (non-persistent)

Designed to run unattended end-to-end::

    python -m experiment.eval.memory_hog.runner            # full run
    python -m experiment.eval.memory_hog.runner --phase eval  # re-eval only
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
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
DEFAULT_EVAL_LLM_REQUEST_TIMEOUT_SECONDS = int(
    os.environ.get("EVAL_LLM_REQUEST_TIMEOUT_SECONDS", "180")
)
DEFAULT_GEMINI_MAX_NEW_TOKENS = int(os.environ.get("GEMINI_MAX_NEW_TOKENS", "512"))


# ── Tiny HTTP helper (no `requests` dep needed) ────────────────────────────
@dataclass
class ApiClient:
    base_url: str
    role: str = "operator"

    def _req(
        self, method: str, path: str, body: Optional[dict] = None, timeout: int = 60
    ) -> Any:
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
            path += "?" + urllib.parse.urlencode(
                {k: v for k, v in q.items() if v is not None}
            )
        return self._req("GET", path)

    def post(
        self,
        path: str,
        body: Optional[dict] = None,
        request_timeout: Optional[int] = None,
        **q: Any,
    ) -> Any:
        if q:
            path += "?" + urllib.parse.urlencode(
                {k: v for k, v in q.items() if v is not None}
            )
        return self._req("POST", path, body=body, timeout=request_timeout or 600)


# ── Chaos-type lookup ────────────────────────────────────────────────────
def _chaos_type_entry(cfg: Dict[str, Any], chaos_type: str) -> Dict[str, Any]:
    """Find the ``chaos_types`` config entry for *chaos_type*."""
    for entry in cfg.get("chaos_types", []) or []:
        if entry.get("name") == chaos_type:
            return entry
    raise KeyError(f"chaos_type {chaos_type!r} not declared in config.chaos_types")


def _fault_category_for(cfg: Dict[str, Any], chaos_type: str) -> str:
    """Return the fault_category declared in config.chaos_types for *chaos_type*.

    Used as ground-truth label when scoring the *cause* dimension of RCA.
    """
    try:
        return _chaos_type_entry(cfg, chaos_type).get("fault_category", "unknown")
    except KeyError:
        return "unknown"


# ── Argo Workflow template priming ──────────────────────────────────────────
# The Litmus plugin injects chaos by *cloning* an existing Argo Workflow named
# ``<service>-<template_suffix>`` from the ``litmus`` namespace. Memory/cpu/
# pod-delete templates were seeded historically via the Litmus UI, but every
# freshly-added fault type (network-latency, network-loss, http-status-code,
# container-kill) has no template until its generated manifest is applied once.
# Without it, those cells fail at injection with "No Argo Workflow found for
# template ..." — which is exactly why they failed in both train and test.
CHAOS_NAMESPACE = "litmus"
_TS_SUFFIX_RE = re.compile(r"-\d{13}$")


def _kubectl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _existing_workflow_bases(namespace: str = CHAOS_NAMESPACE) -> set:
    """Base names (sans ``-<13-digit-ts>`` suffix) of Argo Workflows in
    *namespace* — i.e. the templates ``_submit_workflow`` is able to clone."""
    proc = _kubectl(
        "get",
        "workflows.argoproj.io",
        "-n",
        namespace,
        "-o",
        "jsonpath={.items[*].metadata.name}",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "kubectl get workflows failed (is the cluster reachable?): "
            + proc.stderr.strip()
        )
    return {_TS_SUFFIX_RE.sub("", name) for name in proc.stdout.split()}


def _wait_workflow_terminal(name: str, namespace: str, timeout_s: int) -> str:
    """Poll an Argo Workflow until it reaches a terminal phase; return it."""
    deadline = time.time() + timeout_s
    phase = ""
    while time.time() < deadline:
        proc = _kubectl(
            "get",
            "workflow.argoproj.io",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.phase}",
        )
        phase = proc.stdout.strip()
        if phase in {"Succeeded", "Failed", "Error", "Skipped"}:
            return phase
        time.sleep(5)
    return phase or "Pending"


def ensure_chaos_templates(cfg: Dict[str, Any]) -> None:
    """Generate + apply any missing ``<service>-<suffix>`` workflow templates so
    every train/test cell can be cloned by the platform's Litmus plugin."""
    suffix_by_type = {
        ct["name"]: ct.get("template_suffix", ct["name"])
        for ct in cfg.get("chaos_types", []) or []
    }
    pairs = sorted(
        {
            (cell["service"], cell["chaos_type"])
            for key in ("train_experiments", "test_experiments")
            for cell in cfg.get(key, []) or []
        }
    )

    try:
        existing = _existing_workflow_bases()
    except (RuntimeError, FileNotFoundError, subprocess.SubprocessError) as exc:
        print(f"[prime] WARNING: could not list workflow templates: {exc}")
        print("[prime] skipping priming — cells without a template will fail")
        return

    missing = [
        (service, chaos_type)
        for service, chaos_type in pairs
        if f"{service}-{suffix_by_type.get(chaos_type, chaos_type)}" not in existing
    ]
    if not missing:
        print(f"[prime] all {len(pairs)} workflow template(s) already present")
        return

    print(
        f"[prime] {len(missing)} of {len(pairs)} template(s) missing — generating "
        "+ applying once so the harness can clone them:"
    )
    for service, chaos_type in missing:
        print(f"          - {service}-{suffix_by_type.get(chaos_type, chaos_type)}")

    if any(ct in ("network-latency", "network-loss") for _, ct in missing):
        print(
            "[prime] NOTE: network faults need the host 'sch_netem' qdisc "
            "(run `make chaos-enable-netem`) to actually degrade traffic."
        )

    try:
        from . import generate_chaos_manifests as genmod
    except ImportError:  # running as a script rather than a package module
        import generate_chaos_manifests as genmod  # type: ignore

    results_dir = Path(cfg["output"]["results_dir"])
    if not results_dir.is_absolute():
        results_dir = ROOT / results_dir
    manifests_dir = results_dir / cfg["output"]["manifests_dir"]
    genmod.generate(cfg, manifests_dir)

    prime_timeout = int(cfg.get("platform", {}).get("prime_timeout_seconds", 180))
    applied: List[str] = []
    for service, chaos_type in missing:
        manifest = manifests_dir / f"{service}-{chaos_type}.yaml"
        if not manifest.exists():
            print(f"[prime] ERROR: generated manifest missing: {manifest}")
            continue
        proc = _kubectl("apply", "-f", str(manifest))
        if proc.returncode != 0:
            print(f"[prime] ERROR applying {manifest.name}: {proc.stderr.strip()}")
            continue
        applied.append(f"{service}-{suffix_by_type.get(chaos_type, chaos_type)}")
        print(f"[prime] applied {manifest.name}: {proc.stdout.strip()}")

    # Wait for each priming run to finish so it doesn't overlap the matrix.
    for tmpl in applied:
        phase = _wait_workflow_terminal(tmpl, CHAOS_NAMESPACE, prime_timeout)
        print(f"[prime] {tmpl} priming run -> {phase}")
    print(f"[prime] done — primed {len(applied)} template(s)")


# ── Spec assembly (mirrors the frontend's buildSpec) ───────────────────────
def build_spec(
    cfg: Dict[str, Any], service: str, replica_idx: int, chaos_type: str
) -> Dict[str, Any]:
    """Compose an experiment.v1 spec for one (service, chaos_type, replica) cell."""
    ct = _chaos_type_entry(cfg, chaos_type)
    suffix = ct["template_suffix"]
    short = ct.get("short", suffix.replace("-", ""))
    fault_category = ct.get("fault_category", "unknown")
    pretty = ct.get("name", chaos_type)

    # Keep the corpus boundary stable across TRAIN/TEST and repeated runs.
    # Database version and run_id still distinguish individual executions.
    exp_id = str(cfg.get("experiment_campaign_id") or "multi-chaos-eval-v1")
    chaos_engine = f"{service}-{suffix}"  # Argo workflow template name
    return {
        "schema_version": "1.0.0",
        "experiment": {
            "id": exp_id,
            "name": f"{pretty} · {service} · r{replica_idx}",
            "description_manual": (
                f"{pretty} injection on {service} in {cfg['cluster']['app_namespace']}. "
                f"Replica {replica_idx} of automated multi-chaos evaluation harness."
            ),
            "hypothesis": (
                f"{pretty} on {service} causes container/pod disruption and "
                "transient latency/error spikes on the affected dependency chain."
            ),
            "tags": [
                "chaos",
                "platform-v1",
                pretty,
                f"fault_category:{fault_category}",
                f"target:{service}",
                f"replica:{replica_idx}",
                "harness:multi-chaos-eval",
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
                # ── Golden Signals (Google SRE) ───────────────────────────
                # Four dimensions: traffic, errors, latency, saturation.
                # Saturation is split into CPU and memory because the chaos
                # types we inject (cpu-hog, memory-hog) act on different
                # resources — a single "saturation" number would conflate
                # them. p50/p95/p99 are all reported because cpu throttling
                # often only shows up in the tail (p99) while p95 stays calm.
                "queries": [
                    # ── Traffic ──
                    {
                        "id": "traffic",
                        "query": "sum(rate(request_duration_seconds_count[5m])) by (name)",
                    },
                    # ── Errors ──
                    {
                        "id": "error_rate",
                        "query": (
                            "sum(rate(request_duration_seconds_count"
                            '{status_code=~"5.."}[5m])) by (name)'
                        ),
                    },
                    # ── Latency (RED) — three percentiles ──
                    {
                        "id": "latency_p50",
                        "query": (
                            "histogram_quantile(0.50, sum(rate("
                            "request_duration_seconds_bucket[5m])) by (le,name))"
                        ),
                    },
                    {
                        "id": "latency_p95",
                        "query": (
                            "histogram_quantile(0.95, sum(rate("
                            "request_duration_seconds_bucket[5m])) by (le,name))"
                        ),
                    },
                    {
                        "id": "latency_p99",
                        "query": (
                            "histogram_quantile(0.99, sum(rate("
                            "request_duration_seconds_bucket[5m])) by (le,name))"
                        ),
                    },
                    # ── Network signals ─────────────────────────────────
                    # Interface-level counters are collected by cAdvisor and
                    # grouped by pod in per_label_hotspots. They are kept
                    # target-agnostic so they cannot leak the injected service.
                    {
                        "id": "network_receive_bytes_rate",
                        "query": (
                            "sum(rate(container_network_receive_bytes_total[1m]))"
                        ),
                    },
                    {
                        "id": "network_transmit_bytes_rate",
                        "query": (
                            "sum(rate(container_network_transmit_bytes_total[1m]))"
                        ),
                    },
                    {
                        "id": "network_receive_errors_rate",
                        "query": (
                            "sum(rate(container_network_receive_errors_total[1m]))"
                        ),
                    },
                    {
                        "id": "network_transmit_errors_rate",
                        "query": (
                            "sum(rate(container_network_transmit_errors_total[1m]))"
                        ),
                    },
                    {
                        "id": "network_receive_dropped_rate",
                        "query": (
                            "sum(rate(container_network_receive_packets_dropped_total[1m]))"
                        ),
                    },
                    {
                        "id": "network_transmit_dropped_rate",
                        "query": (
                            "sum(rate(container_network_transmit_packets_dropped_total[1m]))"
                        ),
                    },
                    # ── Saturation: CPU ──
                    # vCPU cores consumed per pod. This cluster's cAdvisor
                    # exposes cpu="total" per pod instead of a container label,
                    # so we filter on cpu="total" and skip the container!="" guard.
                    {
                        "id": "cpu_usage_cores",
                        "query": (
                            "rate(container_cpu_usage_seconds_total"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}",'
                            'cpu="total"}[1m])'
                        ),
                    },
                    # CPU% of limit using spec quota/period (present only when
                    # resource limits are set in the Deployment manifest).
                    {
                        "id": "cpu_saturation_pct",
                        "query": (
                            "100 * rate(container_cpu_usage_seconds_total"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}",'
                            'cpu="total"}[1m]) / on(pod) group_left() '
                            "(container_spec_cpu_quota"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}} '
                            "/ container_spec_cpu_period"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}})'
                        ),
                    },
                    # CPU throttled seconds/s per pod — fires on cpu-hog even
                    # when usage stays flat because k8s clamps the cgroup.
                    # (No cpu="total" filter: throttled metric has no cpu label.)
                    {
                        "id": "cpu_throttled",
                        "query": (
                            "rate(container_cpu_cfs_throttled_seconds_total"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}}[1m])'
                        ),
                    },
                    # ── Saturation: Memory ──
                    # Working set bytes per pod (what the OOM killer looks at).
                    {
                        "id": "memory_working_set_bytes",
                        "query": (
                            "container_memory_working_set_bytes"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}}'
                        ),
                    },
                    # Memory% of limit. Filter denominator > 0 so pods without
                    # memory limits don't produce +Inf (no-limit = no saturation
                    # signal — those pods simply won't appear in this series).
                    {
                        "id": "memory_saturation_pct",
                        "query": (
                            "100 * container_memory_working_set_bytes"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}} '
                            "/ on(pod) group_left() "
                            "(container_spec_memory_limit_bytes"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}} > 0)'
                        ),
                    },
                    # ── Pod health (pod-failure / OOM-kill signal) ──
                    # 1) Restart rate over a 5m window: useful as a "did
                    #    anything restart recently" smoke signal in hotspots.
                    {
                        "id": "pod_restarts",
                        "query": (
                            "sum by (pod) (changes("
                            "kube_pod_container_status_restarts_total"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}}[5m]))'
                        ),
                    },
                    # 2) Cumulative restart counter, range-queried across the
                    #    whole experiment window. We deliberately AVOID
                    #    `increase(...[1h])` here because a 1h lookback would
                    #    smear restarts from earlier experiments into the
                    #    current run. With the raw counter, the engine's
                    #    per-phase summarize gives us min/max per phase, and
                    #    the "restarts during this run" = (post.max or
                    #    fault.max) − baseline.min is naturally scoped to the
                    #    experiment timeline.
                    {
                        "id": "pod_restarts_total",
                        "query": (
                            "sum by (pod) ("
                            "kube_pod_container_status_restarts_total"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}"}})'
                        ),
                    },
                    # 3) OOMKilled flag — gauge that is 1 if the *last*
                    #    container termination reason was OOMKilled. Going
                    #    0→1 within the experiment window is a strong,
                    #    nearly-unambiguous signal of memory exhaustion in
                    #    that pod. Per-phase max captures the transition.
                    {
                        "id": "oom_killed",
                        "query": (
                            "sum by (pod) ("
                            "kube_pod_container_status_last_terminated_reason"
                            f'{{namespace="{cfg["cluster"]["app_namespace"]}",'
                            'reason="OOMKilled"})'
                        ),
                    },
                ],
            },
            "traces": {
                "provider": "mcp-tempo",
                "mcp_sse_url": "http://127.0.0.1:18080/sse",
                "timeout_seconds": 20,
                # Namespace-scoped query (NOT service-scoped). In production
                # RCA the operator does NOT know which service is the culprit
                # — that's what we're trying to predict. Filtering traces by
                # `chaos_target` at collection time would leak ground truth
                # into every downstream artefact (affected_services tag,
                # critical_path, propagation_graph, summary_text, embedding)
                # and inflate retrieval scores via service-name token match.
                # We therefore fetch traces for the whole namespace and let
                # the engine derive affected_services from latency/errors.
                "query": (
                    f"{{ resource.k8s.namespace.name = "
                    f'"{cfg["cluster"]["app_namespace"]}" }}'
                ),
                "service_name": None,
                "limit": 50,
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
    phase: str  # "train" or "test"
    chaos_target: str
    experiment_id: str
    run_id: Optional[str] = None
    status: str = "pending"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    notes: str = ""
    chaos_type: str = "memory-hog"  # injected fault type (default = legacy)


def create_and_start_run(
    api: ApiClient,
    cfg: Dict[str, Any],
    service: str,
    replica_idx: int,
    phase: str,
    chaos_type: str,
) -> RunRecord:
    spec = build_spec(cfg, service, replica_idx, chaos_type)
    rec = RunRecord(
        service=service,
        replica_idx=replica_idx,
        phase=phase,
        chaos_target=service,
        experiment_id=spec["experiment"]["id"],
        chaos_type=chaos_type,
    )
    # 1. Create experiment (returns id + version)
    api.post(
        "/api/experiments",
        body={
            "initiated_by": cfg["platform"]["initiated_by"],
            "spec": spec,
        },
    )
    # 2. Start run
    resp = api.post(
        "/api/runs/start",
        body={
            "experiment_id": spec["experiment"]["id"],
            "initiated_by": cfg["platform"]["initiated_by"],
            "idempotency_key": f"{spec['experiment']['id']}-{uuid.uuid4().hex[:8]}",
            # Flag the run as training-corpus or held-out test. The backend uses
            # this to gate RAG retrieval so test rows never leak as neighbours.
            "is_training": (phase == "train"),
        },
    )
    rec.run_id = resp["run_id"]
    rec.status = resp["status"]
    return rec


def wait_for_run(
    api: ApiClient, run_id: str, *, poll_s: int, timeout_s: int
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = api.get(f"/api/runs/{run_id}")
        if last.get("status") in TERMINAL_STATUSES:
            return last
        time.sleep(poll_s)
    raise TimeoutError(
        f"Run {run_id} did not reach a terminal state within {timeout_s}s"
    )


# ── Evaluation ─────────────────────────────────────────────────────────────
@dataclass
class StrategyResult:
    run_id: str
    chaos_target: str
    expected_fault_category: str
    strategy: str
    predicted_rca: Optional[str]
    correct: Optional[bool]  # alias of correct_target (back-compat)
    correct_target: Optional[bool]  # WHERE: predicted service == injected service
    correct_fault: Optional[
        bool
    ]  # WHY:   predicted fault_category == injected category
    correct_full: Optional[bool]  # both target AND fault correct
    confidence: Optional[float]
    fault_category: Optional[str]  # predicted fault_category from LLM
    prompt_tokens: Optional[int]
    prompt_sha256: Optional[str]
    backend_timing_ms: Optional[str]
    neighbours_used: Optional[int]
    rag_mode: Optional[str]
    system_card_id: Optional[str]
    # FaultCategoryValidator outputs (rule-based post-processor; may have
    # overridden `fault_category` above). Empty when no run / cached.
    validator_fired: Optional[bool] = None
    validator_rule: Optional[str] = None
    validator_original_fault: Optional[str] = None
    validator_conflict: Optional[str] = None
    # ServiceLocalizerValidator outputs (may have overridden `rca`).
    localizer_fired: Optional[bool] = None
    localizer_rule: Optional[str] = None
    localizer_original_rca: Optional[str] = None
    # Retry count emitted by the API when the first LLM attempt failed
    # (parse error or empty answer). 0 = first attempt was usable.
    retry_count: Optional[int] = None
    raw_error: str = ""


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def evaluate_strategy(
    api: ApiClient,
    run: RunRecord,
    strategy: Dict[str, Any],
    expected_fault: str,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> StrategyResult:
    """Score one (run × strategy) cell on two RCA dimensions:

    - *correct_target*: localisation — did the LLM point at the right service?
    - *correct_fault*:  cause       — did the LLM name the right fault category?
    - *correct_full*:   conjunction — both correct.

    *correct* is kept as an alias of *correct_target* for back-compat with
    older evaluation.csv consumers (notebooks, etc.).
    """
    name = strategy["name"]
    max_new_tokens = int(strategy.get("max_new_tokens", 512))
    request_timeout = int(
        os.environ.get(
            "EVAL_LLM_REQUEST_TIMEOUT_SECONDS",
            str(DEFAULT_EVAL_LLM_REQUEST_TIMEOUT_SECONDS),
        )
    )
    if llm_provider and "gemini" in llm_provider.lower():
        gemini_token_cap = int(
            os.environ.get("GEMINI_MAX_NEW_TOKENS", str(DEFAULT_GEMINI_MAX_NEW_TOKENS))
        )
        max_new_tokens = min(max_new_tokens, gemini_token_cap)
    print(
        "         request "
        f"provider={llm_provider or 'default'} model={llm_model or 'default'} "
        f"mode={strategy['mode']} limit={strategy['limit']} "
        f"budget_tokens={strategy['budget_tokens']} max_new_tokens={max_new_tokens} "
        f"timeout={request_timeout}s",
        flush=True,
    )
    started = time.monotonic()
    try:
        resp = api.post(
            f"/api/runs/{run.run_id}/llm-analysis",
            request_timeout=request_timeout,
            force="true",
            limit=strategy["limit"],
            mode=strategy["mode"],
            system_id=strategy["system_id"],
            budget_tokens=strategy["budget_tokens"],
            max_new_tokens=max_new_tokens,
            provider=llm_provider,
            model=llm_model,
            eval_target=run.chaos_target,
            eval_fault=expected_fault,
            eval_strategy=name,
        )
    except Exception as exc:  # pragma: no cover - network/LLM failures
        elapsed = time.monotonic() - started
        print(f"         failed after {elapsed:.1f}s: {exc}", flush=True)
        return StrategyResult(
            run_id=run.run_id or "",
            chaos_target=run.chaos_target,
            expected_fault_category=expected_fault,
            strategy=name,
            predicted_rca=None,
            correct=None,
            correct_target=None,
            correct_fault=None,
            correct_full=None,
            confidence=None,
            fault_category=None,
            prompt_tokens=None,
            prompt_sha256=None,
            backend_timing_ms=None,
            neighbours_used=None,
            rag_mode=None,
            system_card_id=None,
            raw_error=str(exc)[:500],
        )
    elapsed = time.monotonic() - started
    print(f"         response after {elapsed:.1f}s", flush=True)

    analysis = resp.get("analysis", {}) if isinstance(resp, dict) else {}
    predicted = _safe_get(analysis, "rca")
    predicted_fault = _safe_get(analysis, "fault_category")
    v_meta = analysis.get("validator_meta") or {}
    v_conflict = v_meta.get("conflict")
    if isinstance(v_conflict, list):
        v_conflict_str: Optional[str] = "|".join(v_conflict)
    else:
        v_conflict_str = None
    loc_meta = v_meta.get("localizer") or {}

    correct_target = (predicted == run.chaos_target) if predicted else False
    # Fault-category match: tolerant to case/whitespace/separator differences
    # (e.g. "memory-exhaustion" vs "memory_exhaustion" vs "Memory Exhaustion").
    correct_fault: Optional[bool]
    if predicted_fault is None:
        correct_fault = False
    else:

        def norm(s: Any) -> str:
            return str(s).strip().lower().replace("_", "-").replace(" ", "-")

        correct_fault = norm(predicted_fault) == norm(expected_fault)
    correct_full = bool(correct_target and correct_fault)
    timing_meta = _safe_get(analysis, "timing_meta", default={}) or {}
    backend_timing_ms = None
    if isinstance(timing_meta, dict) and timing_meta:
        backend_timing_ms = ",".join(
            f"{key}={timing_meta.get(key)}"
            for key in ("rag_ms", "prompt_ms", "llm_ms", "parse_ms", "total_ms")
        )
        print(f"         backend_timing {backend_timing_ms}", flush=True)

    return StrategyResult(
        run_id=run.run_id or "",
        chaos_target=run.chaos_target,
        expected_fault_category=expected_fault,
        strategy=name,
        predicted_rca=predicted,
        correct=correct_target,
        correct_target=correct_target,
        correct_fault=correct_fault,
        correct_full=correct_full,
        confidence=_safe_get(analysis, "confidence"),
        fault_category=predicted_fault,
        prompt_tokens=_safe_get(analysis, "prompt_meta", "prompt_tokens_estimate"),
        prompt_sha256=_safe_get(analysis, "prompt_meta", "prompt_sha256"),
        backend_timing_ms=backend_timing_ms,
        neighbours_used=_safe_get(analysis, "prompt_meta", "neighbours_used"),
        rag_mode=_safe_get(analysis, "rag_mode"),
        system_card_id=_safe_get(analysis, "prompt_meta", "system_card_id"),
        validator_fired=bool(v_meta.get("fired")) if v_meta else None,
        validator_rule=v_meta.get("rule"),
        validator_original_fault=v_meta.get("original_fault_category"),
        validator_conflict=v_conflict_str,
        localizer_fired=bool(loc_meta.get("fired")) if loc_meta else None,
        localizer_rule=loc_meta.get("rule"),
        localizer_original_rca=loc_meta.get("original_rca"),
        retry_count=_safe_get(analysis, "retry_count"),
    )


# ── CSV writers ─────────────────────────────────────────────────────────────
RUN_FIELDS = [
    "phase",
    "chaos_type",
    "service",
    "replica_idx",
    "chaos_target",
    "experiment_id",
    "run_id",
    "status",
    "started_at",
    "ended_at",
    "notes",
]
EVAL_FIELDS = [
    "run_id",
    "chaos_target",
    "expected_fault_category",
    "strategy",
    "predicted_rca",
    "fault_category",
    "correct",
    "correct_target",
    "correct_fault",
    "correct_full",
    "confidence",
    "prompt_tokens",
    "prompt_sha256",
    "backend_timing_ms",
    "neighbours_used",
    "rag_mode",
    "system_card_id",
    "validator_fired",
    "validator_rule",
    "validator_original_fault",
    "validator_conflict",
    "localizer_fired",
    "localizer_rule",
    "localizer_original_rca",
    "retry_count",
    "raw_error",
]


def _write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _load_runs_csv(path: Path) -> List[RunRecord]:
    if not path.exists():
        return []
    out: List[RunRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(
                RunRecord(
                    service=r["service"],
                    replica_idx=int(r["replica_idx"]),
                    phase=r["phase"],
                    chaos_target=r["chaos_target"],
                    experiment_id=r["experiment_id"],
                    run_id=r.get("run_id") or None,
                    status=r.get("status") or "unknown",
                    started_at=r.get("started_at") or None,
                    ended_at=r.get("ended_at") or None,
                    notes=r.get("notes") or "",
                    # Back-compat: rows from the single-chaos era default to memory-hog
                    chaos_type=(r.get("chaos_type") or "memory-hog"),
                )
            )
    return out


# ── Main orchestration ─────────────────────────────────────────────────────
def _flush_runs(runs_csv: Path, records: List[RunRecord]) -> None:
    """Persist runs.csv after every cell so a Ctrl-C doesn't lose progress."""
    _write_csv(runs_csv, [r.__dict__ for r in records], RUN_FIELDS)


def _run_chaos_loop(
    api: ApiClient,
    cfg: Dict[str, Any],
    cells: List[Dict[str, str]],
    replicas: int,
    phase: str,
    log_prefix: str,
    completed: Dict[tuple, RunRecord],
    all_records: List[RunRecord],
    runs_csv: Path,
) -> None:
    """Run chaos for every (service, chaos_type, replica) cell.

    *cells* is a list of ``{"service": ..., "chaos_type": ...}`` dicts.
    Cells whose key is present in *completed* (i.e. already 'completed' in a
    prior runs.csv) are skipped and the prior record is kept verbatim.
    Progress is flushed to *runs_csv* after every cell.
    """
    poll_s = int(cfg["platform"]["poll_interval_seconds"])
    timeout_s = int(cfg["platform"]["run_timeout_seconds"])
    for cell in cells:
        service = cell["service"]
        chaos_type = cell["chaos_type"]
        for rep in range(replicas):
            key = (phase, chaos_type, service, rep)
            prior = completed.get(key)
            if prior is not None:
                print(
                    f"  [{log_prefix}] skip   chaos={chaos_type} service={service} "
                    f"replica={rep} (already completed run_id={prior.run_id})"
                )
                all_records.append(prior)
                _flush_runs(runs_csv, all_records)
                continue
            print(
                f"  [{log_prefix}] start  chaos={chaos_type} service={service} "
                f"replica={rep}"
            )
            try:
                rec = create_and_start_run(api, cfg, service, rep, phase, chaos_type)
            except Exception as exc:
                rec = RunRecord(
                    service=service,
                    replica_idx=rep,
                    phase=phase,
                    chaos_target=service,
                    experiment_id="",
                    notes=f"start_failed: {exc}"[:500],
                    chaos_type=chaos_type,
                )
                all_records.append(rec)
                _flush_runs(runs_csv, all_records)
                print(
                    f"  [{log_prefix}] FAIL   chaos={chaos_type} service={service} "
                    f"replica={rep}: {exc}"
                )
                continue
            try:
                final = wait_for_run(
                    api, rec.run_id, poll_s=poll_s, timeout_s=timeout_s
                )
                rec.status = final.get("status", rec.status)
                rec.started_at = final.get("started_at")
                rec.ended_at = final.get("ended_at")
                print(
                    f"  [{log_prefix}] done   run_id={rec.run_id} status={rec.status}"
                )
            except Exception as exc:
                rec.notes = f"wait_failed: {exc}"[:500]
                print(f"  [{log_prefix}] TIMEOUT run_id={rec.run_id}: {exc}")
            all_records.append(rec)
            _flush_runs(runs_csv, all_records)
            # Cooldown between runs: give pods time to fully recover (restart
            # backoff, readinessProbe pass) before the next chaos injection.
            # Without this, a pod OOM-killed by memory-hog may still be in
            # CrashLoopBackOff when the next run's baseline phase samples
            # metrics, inflating the background error_rate artificially.
            cooldown_s = int(cfg["platform"].get("inter_run_cooldown_seconds", 0))
            if cooldown_s > 0:
                print(f"  [{log_prefix}] cooldown {cooldown_s}s …")
                time.sleep(cooldown_s)


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
                completed[(rec.phase, rec.chaos_type, rec.service, rec.replica_idx)] = (
                    rec
                )
        print(
            f"[retry-failed] keeping {len(completed)} completed cell(s) "
            f"from previous run"
        )

    all_records: List[RunRecord] = []
    print("--- PHASE 1: TRAIN (RAG corpus seeding) ---")
    _run_chaos_loop(
        api,
        cfg,
        cfg["train_experiments"],
        int(cfg["replicas"]),
        "train",
        "TRAIN",
        completed,
        all_records,
        runs_csv,
    )
    print("--- PHASE 2: TEST (held-out runs) ---")
    _run_chaos_loop(
        api,
        cfg,
        cfg["test_experiments"],
        int(cfg["replicas"]),
        "test",
        "TEST ",
        completed,
        all_records,
        runs_csv,
    )
    print(f"Wrote {len(all_records)} run records to {_display_path(runs_csv)}")
    return all_records


def phase_eval(
    api: ApiClient,
    cfg: Dict[str, Any],
    runs: List[RunRecord],
    eval_csv: Path,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> List[StrategyResult]:
    print("─── PHASE 3: EVAL (strategy sweep on test runs) ─────────────────")
    test_runs = [
        r for r in runs if r.phase == "test" and r.run_id and r.status == "completed"
    ]
    if not test_runs:
        print("No completed test runs to evaluate.")
        return []

    results: List[StrategyResult] = []
    for run in test_runs:
        # Prefer the ground truth that the engine persisted on the run row
        # (auto-extracted from experiment tags). Fallback to the static
        # chaos_type → fault_category map only if the row predates the
        # auto-labelling change.
        expected_fault: Optional[str] = None
        gt_service_persisted: Optional[str] = None
        try:
            feats = api.get(f"/api/runs/{run.run_id}/features") or {}
            expected_fault = feats.get("ground_truth_fault_category") or None
            gt_service_persisted = feats.get("ground_truth_service") or None
        except Exception:
            feats = {}
        if not expected_fault:
            expected_fault = _fault_category_for(cfg, run.chaos_type)
        # Sanity check: if the run row carries a different chaos_target than
        # the harness CSV (unlikely but possible after manual edits), trust
        # the persisted one — it came from the experiment tag at submit time.
        if gt_service_persisted and gt_service_persisted != run.chaos_target:
            print(
                f"  [WARN ] run={run.run_id} chaos_target mismatch: "
                f"csv={run.chaos_target} persisted={gt_service_persisted} "
                f"(using persisted)"
            )
            run.chaos_target = gt_service_persisted
        for strategy in cfg["strategies"]:
            if llm_provider and "gemini" in llm_provider.lower():
                delay_s = float(os.environ.get("GEMINI_EVAL_DELAY_SECONDS", "0"))
                if delay_s > 0:
                    time.sleep(delay_s)
            print(
                f"  [EVAL ] run={run.run_id} target={run.chaos_target} "
                f"fault={expected_fault} strategy={strategy['name']}"
            )
            res = evaluate_strategy(
                api,
                run,
                strategy,
                expected_fault,
                llm_provider=llm_provider,
                llm_model=llm_model,
            )
            results.append(res)
            if res.raw_error:
                print(f"         error: {res.raw_error}")
            else:
                print(
                    f"         predicted={res.predicted_rca} "
                    f"fault={res.fault_category} "
                    f"target_ok={res.correct_target} "
                    f"fault_ok={res.correct_fault} "
                    f"full_ok={res.correct_full} "
                    f"conf={res.confidence} tokens={res.prompt_tokens} "
                    f"prompt_sha={res.prompt_sha256 or 'n/a'}"
                )
    _write_csv(eval_csv, [r.__dict__ for r in results], EVAL_FIELDS)
    print(f"Wrote {len(results)} evaluation rows to {_display_path(eval_csv)}")
    _print_summary(results, cfg)
    return results


def _norm(s: Any) -> str:
    """Normalise a fault/service label for tolerant comparison."""
    return str(s).strip().lower().replace("_", "-").replace(" ", "-")


def _compute_prediction_scores(
    *,
    raw_rca: Optional[str],
    raw_fault_category: Optional[str],
    final_rca: Optional[str],
    final_fault_category: Optional[str],
    chaos_target: str,
    expected_fault_category: str,
) -> tuple[dict[str, bool], dict[str, bool]]:
    """Return the raw-model and final-pipeline scores for one result.

    The raw model prediction is the answer *before* deterministic post-
    processing. The final scores represent the answer *after* validator/localizer
    overrides, which is what the operational pipeline actually reports.
    """

    def _score(rca: Optional[str], fault: Optional[str]) -> dict[str, bool]:
        target_ok = bool(rca) and _norm(rca) == _norm(chaos_target)
        fault_ok = bool(fault) and _norm(fault) == _norm(expected_fault_category)
        return {
            "correct_target": target_ok,
            "correct_fault": fault_ok,
            "correct_full": target_ok and fault_ok,
        }

    return _score(raw_rca, raw_fault_category), _score(final_rca, final_fault_category)


def _raw_scores(r: StrategyResult, chaos_target: str) -> tuple:
    """Recover the *pre-override* LLM answer for one strategy result.

    The deterministic ``fault_category_validator`` / ``service_localizer``
    post-processors overwrite the LLM's ``fault_category`` / ``rca`` when a
    metric fingerprint matches. When they fire, the final answer is
    metric-derived and therefore identical regardless of the RAG strategy —
    which is why the post-override table can look the same for every strategy.
    Here we read back the raw LLM guess (the validator/localizer record their
    ``original_*`` value only when they fired; otherwise the final value *is*
    the raw one) so per-strategy divergence is visible.

    Returns ``(raw_target_ok, raw_fault_ok, raw_full_ok)``.
    """
    raw_rca = r.localizer_original_rca if r.localizer_fired else r.predicted_rca
    raw_fault = r.validator_original_fault if r.validator_fired else r.fault_category
    raw_scores, _ = _compute_prediction_scores(
        raw_rca=raw_rca,
        raw_fault_category=raw_fault,
        final_rca=r.predicted_rca,
        final_fault_category=r.fault_category,
        chaos_target=chaos_target,
        expected_fault_category=r.expected_fault_category,
    )
    return (
        raw_scores["correct_target"],
        raw_scores["correct_fault"],
        raw_scores["correct_full"],
    )


def _print_accuracy_table(
    grouped: Dict[str, List[StrategyResult]],
    cfg: Dict[str, Any],
    *,
    raw: bool,
) -> None:
    print(
        f"{'strategy':<32} {'n':>4} {'where':>8} {'why':>8} {'both':>8} {'avg_tok':>10}"
    )
    for strat in cfg["strategies"]:
        name = strat["name"]
        rows = grouped.get(name, [])
        if not rows:
            continue
        valid = [r for r in rows if r.predicted_rca is not None]
        n = len(rows)
        if raw:
            scored = [_raw_scores(r, r.chaos_target) for r in rows]
            acc_target = sum(1 for s in scored if s[0]) / n if n else 0.0
            acc_fault = sum(1 for s in scored if s[1]) / n if n else 0.0
            acc_full = sum(1 for s in scored if s[2]) / n if n else 0.0
        else:
            final_scores = []
            for r in rows:
                _, score = _compute_prediction_scores(
                    raw_rca=(
                        r.localizer_original_rca
                        if r.localizer_fired
                        else r.predicted_rca
                    ),
                    raw_fault_category=(
                        r.validator_original_fault
                        if r.validator_fired
                        else r.fault_category
                    ),
                    final_rca=r.predicted_rca,
                    final_fault_category=r.fault_category,
                    chaos_target=r.chaos_target,
                    expected_fault_category=r.expected_fault_category,
                )
                final_scores.append(score)
            acc_target = (
                sum(1 for s in final_scores if s["correct_target"]) / n if n else 0.0
            )
            acc_fault = (
                sum(1 for s in final_scores if s["correct_fault"]) / n if n else 0.0
            )
            acc_full = (
                sum(1 for s in final_scores if s["correct_full"]) / n if n else 0.0
            )
        avg_tok = (
            (sum((r.prompt_tokens or 0) for r in valid) / len(valid)) if valid else 0.0
        )
        print(
            f"{name:<32} {n:>4} {acc_target:>8.2%} {acc_fault:>8.2%} "
            f"{acc_full:>8.2%} {avg_tok:>10.0f}"
        )


def _print_summary(results: List[StrategyResult], cfg: Dict[str, Any]) -> None:
    grouped: Dict[str, List[StrategyResult]] = {}
    for r in results:
        grouped.setdefault(r.strategy, []).append(r)

    # How often the deterministic post-processors overrode the LLM — this is
    # what collapses the strategies to identical scores in the final table.
    fired = sum(1 for r in results if r.validator_fired or r.localizer_fired)
    total = len(results)

    print()
    print("─── Summary: FINAL RCA accuracy per strategy (post-override) ─────")
    print("    where = service localisation | why = fault_category | both = full RCA")
    _print_accuracy_table(grouped, cfg, raw=False)

    print()
    print("─── Summary: RAW LLM accuracy per strategy (pre-override) ────────")
    print(
        f"    deterministic validator/localizer overrode {fired}/{total} "
        "result(s); where they fire, every strategy collapses to the same"
    )
    print("    metric-derived answer. The raw table below shows true divergence.")
    _print_accuracy_table(grouped, cfg, raw=True)


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(CONFIG_PATH))
    p.add_argument(
        "--phase",
        choices=["all", "chaos", "eval"],
        default="all",
        help="Phase to run. 'eval' reuses runs.csv from a previous chaos phase.",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-run only chaos cells that are not 'completed' in runs.csv; "
        "completed cells are kept verbatim.",
    )
    p.add_argument(
        "--skip-prime",
        action="store_true",
        help="Skip auto-generating/applying missing Argo Workflow templates "
        "before the chaos phase (assume they already exist in the cluster).",
    )
    p.add_argument(
        "--llm-provider",
        choices=["qwen", "gpt", "gpt-4o-mini", "openai", "github", "gemini"],
        default=None,
        help="LLM provider for evaluation phase. Default: qwen (local). "
        "Use 'gemini' for Google Gemini or 'gpt' for OpenAI.",
    )
    p.add_argument(
        "--llm-model",
        default=None,
        help="Override LLM model name (e.g., gpt-4o-mini, qwen-14b).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the spec for the first cell and exit.",
    )
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
        cell = cfg["train_experiments"][0]
        spec = build_spec(cfg, cell["service"], 0, cell["chaos_type"])
        print(json.dumps(spec, indent=2))
        return 0

    # Health check first — fail fast if the platform isn't running.
    try:
        api.get("/api/system-cards")
    except Exception as exc:
        print(
            f"Cannot reach platform at {cfg['platform']['api_base']}: {exc}",
            file=sys.stderr,
        )
        return 2

    runs: List[RunRecord] = []
    if args.phase in ("all", "chaos"):
        if not args.skip_prime:
            ensure_chaos_templates(cfg)
        runs = phase_chaos(api, cfg, runs_csv, retry_failed=args.retry_failed)
    else:
        runs = _load_runs_csv(runs_csv)
        if not runs:
            print(
                f"--phase eval needs an existing {runs_csv}; run --phase chaos first",
                file=sys.stderr,
            )
            return 2

    if args.phase in ("all", "eval"):
        phase_eval(
            api,
            cfg,
            runs,
            eval_csv,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
