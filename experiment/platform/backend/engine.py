"""Orchestrator engine: the application core that runs experiments.

``OrchestratorEngine`` coordinates the chaos, load, metrics and trace
provider ports, drives a run through its lifecycle (start → inject chaos →
collect → analyse → conclude) and persists state via the storage port. It
depends only on the port Protocols, never on concrete adapters.
"""

from __future__ import annotations

import threading
import time
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .analysis import (
    anonymize_services,
    build_observability_summary,
    build_run_summary_l2,
    compute_temporal_features,
    get_embedding_provider,
    vector_to_blob,
)
from .domain import DomainEvent, ExperimentVersion, RunRecord, RunStatus, utc_now_iso
from .ports import (
    ChaosProviderPort,
    HistoryMigrationPort,
    LoadProviderPort,
    MetricsProviderPort,
    NotificationPort,
    RcaEvidenceSnapshotPort,
    StoragePort,
    TraceProviderPort,
)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration_seconds(start_iso: str, end_iso: str) -> float:
    return (_parse_iso(end_iso) - _parse_iso(start_iso)).total_seconds()


def _resolve_fault_window(
    planned_start: str,
    planned_end: str,
    chaos_artifacts: Dict[str, Any],
    configured_duration_seconds: Optional[int],
) -> tuple[str, str, str]:
    """Prefer observed injection timestamps, falling back to the planned phase."""
    if chaos_artifacts.get("mode") == "sham":
        return planned_start, planned_end, "sham_planned_phase"
    if chaos_artifacts.get("mode") == "manifest":
        actual_start = chaos_artifacts.get("injected_at")
        actual_end = chaos_artifacts.get("stopped_at")
        if actual_start and actual_end and _duration_seconds(actual_start, actual_end) > 0:
            return actual_start, actual_end, "manifest_observed"

    final_status = chaos_artifacts.get("final_status") or {}
    actual_start = (
        final_status.get("chaos_started_at")
        if final_status.get("chaos_window_confirmed")
        else None
    )
    if actual_start and configured_duration_seconds and configured_duration_seconds > 0:
        actual_end = (
            _parse_iso(actual_start)
            + timedelta(seconds=int(configured_duration_seconds))
        ).isoformat()
        stopped_at = chaos_artifacts.get("stopped_at")
        if stopped_at and _parse_iso(stopped_at) < _parse_iso(actual_end):
            actual_end = stopped_at
        if _duration_seconds(actual_start, actual_end) > 0:
            return (
                actual_start,
                actual_end,
                "litmus_result_created_at_plus_configured_duration",
            )

    # The Argo run-chaos step starts before the experiment pod; it is a
    # coarser anchor but still closer to the injection than the schedule.
    step_start = final_status.get("chaos_step_started_at")
    if step_start and configured_duration_seconds and configured_duration_seconds > 0:
        step_end = (
            _parse_iso(step_start) + timedelta(seconds=int(configured_duration_seconds))
        ).isoformat()
        step_finished = final_status.get("chaos_step_finished_at")
        if step_finished and _parse_iso(step_finished) < _parse_iso(step_end):
            step_end = step_finished
        if _duration_seconds(step_start, step_end) > 0:
            return step_start, step_end, "argo_step_started_at_plus_configured_duration"

    return planned_start, planned_end, "planned_phase_fallback"


def _build_observation_window(
    run_id: str,
    experiment_id: str,
    status: str,
    timings: Dict[str, Any],
    sampling_interval_seconds: int,
) -> Dict[str, Any]:
    """Build the compact, authoritative time contract for a completed run."""
    controlled = timings.get("controlled_observation_window")
    if controlled is None:
        def timestamp(name: str) -> str | None:
            marker = timings.get(name)
            return marker.get("at") if isinstance(marker, dict) else None

        window_start = timestamp("warmup_started")
        window_end = timestamp("recovery_observed")
        estimate = timings.get("intervention_estimate")
        if window_start and window_end and isinstance(estimate, dict):
            phase_bounds = {
                "warmup": (window_start, timestamp("warmup_completed")),
                "baseline": (timestamp("baseline_started"), timestamp("baseline_completed")),
                "fault": (estimate.get("start"), estimate.get("end")),
                "post": (timestamp("intervention_removed"), window_end),
            }
            controlled = {
                "timezone": "UTC",
                "sampling_interval_seconds": sampling_interval_seconds or 5,
                "window": {"start": window_start, "end": window_end},
                "phases": {
                    name: {"start": start, "end": end}
                    for name, (start, end) in phase_bounds.items()
                    if isinstance(start, str) and isinstance(end, str)
                },
                "phase_sources": {
                    "baseline": "after_load_stabilization",
                    "fault": estimate.get("source", "unknown"),
                },
            }
    if controlled is not None:
        if not isinstance(controlled, dict):
            raise ValueError("controlled observation window must be an object")
        window = controlled.get("window")
        phases = controlled.get("phases")
        phase_sources = controlled.get("phase_sources")
        if (
            not isinstance(window, dict)
            or not all(isinstance(window.get(key), str) and window[key] for key in ("start", "end"))
            or not isinstance(phases, dict)
            or not isinstance(phase_sources, dict)
        ):
            raise ValueError("controlled observation window requires valid bounds, phases, and sources")
        return {
            "schema_version": "1",
            "run_id": run_id,
            "experiment_id": experiment_id,
            "status": status,
            "timezone": controlled.get("timezone", "UTC"),
            "window": window,
            "phases": phases,
            "phase_sources": phase_sources,
            "sampling_interval_seconds": (
                sampling_interval_seconds
                or int(controlled.get("sampling_interval_seconds", 0) or 0)
            ),
        }

    phase_keys = {
        "baseline": ("t_start", "t_warmup_start"),
        "warmup": ("t_warmup_start", "t_warmup_end"),
        "fault": ("t_fault_start", "t_fault_end"),
        "post": ("t_fault_end", "t_recovery_observed"),
    }
    phases = {
        name: {"start": timings[start_key], "end": timings[end_key]}
        for name, (start_key, end_key) in phase_keys.items()
        if timings.get(start_key) and timings.get(end_key)
    }

    start = timings.get("t_start")
    end = timings.get("t_recovery_observed")
    if not start or not end:
        raise ValueError("observation window requires start and end timestamps")

    return {
        "schema_version": "1",
        "run_id": run_id,
        "experiment_id": experiment_id,
        "status": status,
        "timezone": "UTC",
        "window": {"start": start, "end": end},
        "phases": phases,
        "phase_sources": {
            "fault": timings.get("fault_window_source", "unknown"),
        },
        "sampling_interval_seconds": sampling_interval_seconds,
    }


def _build_partial_observation_window(
    run_id: str,
    experiment_id: str,
    status: str,
    events: list[DomainEvent],
    ended_at: str,
    sampling_interval_seconds: int,
) -> Dict[str, Any]:
    """Build a best-effort window for stopped or failed runs."""
    event_times = {event.event_type: event.ts for event in events}
    start = event_times.get("run_started")
    if not start:
        raise ValueError("partial observation window requires run_started event")

    phases = {}
    phase_events = {
        "baseline": ("run_started", "warmup_started"),
        "warmup": ("warmup_started", "warmup_completed"),
        "fault": ("fault_started", "fault_completed"),
        "post": ("recovery_observation_started", "recovery_observation_completed"),
    }
    for name, (start_event, end_event) in phase_events.items():
        phase_start = event_times.get(start_event)
        phase_end = event_times.get(end_event)
        if phase_start and phase_end:
            phases[name] = {"start": phase_start, "end": phase_end}

    return {
        "schema_version": "1",
        "run_id": run_id,
        "experiment_id": experiment_id,
        "status": status,
        "timezone": "UTC",
        "window": {"start": start, "end": ended_at},
        "phases": phases,
        "sampling_interval_seconds": sampling_interval_seconds,
        "partial": True,
    }


class OrchestratorEngine:
    def __init__(
        self,
        storage: StoragePort,
        chaos: ChaosProviderPort,
        load: LoadProviderPort,
        metrics: MetricsProviderPort,
        traces: TraceProviderPort,
        notifier: NotificationPort,
        kill_switch_path: Optional[Path] = None,
        evidence_snapshot: Optional[RcaEvidenceSnapshotPort] = None,
        history_migration: Optional[HistoryMigrationPort] = None,
    ):
        self.storage = storage
        self.chaos = chaos
        self.load = load
        self.metrics = metrics
        self.traces = traces
        self.notifier = notifier
        self.kill_switch_path = kill_switch_path
        self.evidence_snapshot = evidence_snapshot
        self.history_migration = history_migration

        self._stop_flags: Dict[str, threading.Event] = {}
        self._runtime_handles: Dict[str, Dict[str, Any]] = {}

    def start_run(
        self,
        exp: ExperimentVersion,
        initiated_by: str,
        idempotency_key: Optional[str] = None,
        approved_by: Optional[str] = None,
        is_training: bool = True,
    ) -> RunRecord:
        gov = exp.spec.get("governance", {})
        requires_approval = bool(gov.get("requires_approval", False))
        initial_status = (
            RunStatus.PENDING_APPROVAL
            if requires_approval and not approved_by
            else RunStatus.PENDING
        )

        run = RunRecord(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            experiment_id=exp.experiment_id,
            experiment_version=exp.version,
            status=initial_status,
            initiated_by=initiated_by,
            started_at=utc_now_iso(),
        )
        # Stash on summary so it survives across the run lifecycle without
        # adding a new column to the runs table.
        run.summary["is_training"] = bool(is_training)
        requested_run_id = run.run_id
        run = self.storage.create_run(run, idempotency_key=idempotency_key)
        if run.run_id != requested_run_id:
            return run

        self._event(
            run.run_id,
            "run_created",
            {"status": run.status.value, "initiated_by": initiated_by},
        )

        if run.status == RunStatus.PENDING_APPROVAL:
            self.notifier.notify(
                "approval_required",
                {"run_id": run.run_id, "experiment_id": exp.experiment_id},
            )
            return run

        stop_flag = threading.Event()
        self._stop_flags[run.run_id] = stop_flag
        t = threading.Thread(
            target=self._execute_run, args=(run.run_id, exp), daemon=True
        )
        t.start()
        return run

    def _persist_rca_evidence_snapshot(
        self,
        run_id: str,
        context: Dict[str, Any],
        observability_config: Dict[str, Any],
        namespace: str,
        observation_window: Dict[str, Any],
        capture_config: Dict[str, Any],
        artifact_paths: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(capture_config, dict) or capture_config.get("enabled") is not True:
            return None
        if self.evidence_snapshot is None:
            snapshot = {
                "schema_version": 1,
                "status": "tool_unavailable",
                "namespace": namespace,
            }
        else:
            try:
                snapshot = self.evidence_snapshot.capture(
                    context,
                    observability_config,
                    namespace,
                    observation_window,
                    capture_config,
                )
            except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
                return None
        if not isinstance(snapshot, dict):
            snapshot = {"schema_version": 1, "status": "invalid_response"}
        snapshot.setdefault("schema_version", 1)
        snapshot["references"] = {
            name: path
            for name, path in artifact_paths.items()
            if name in {"metrics_raw", "traces_raw", "observation_window"}
        }
        try:
            artifact_paths["rca_evidence_snapshot"] = self.storage.save_artifact(
                run_id, "rca_evidence_snapshot", snapshot
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return None
        return snapshot

    def approve_run(self, run_id: str, approved_by: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.status != RunStatus.PENDING_APPROVAL:
            return run

        run.status = RunStatus.PENDING
        self.storage.update_run(run)
        exp = self.storage.get_experiment(run.experiment_id, run.experiment_version)

        stop_flag = threading.Event()
        self._stop_flags[run.run_id] = stop_flag
        t = threading.Thread(
            target=self._execute_run, args=(run.run_id, exp), daemon=True
        )
        t.start()

        self._event(run_id, "run_approved", {"approved_by": approved_by})
        return self.storage.get_run(run_id)

    def stop_run(
        self, run_id: str, requested_by: str, reason: str = "manual_stop"
    ) -> RunRecord:
        flag = self._stop_flags.get(run_id)
        if flag:
            flag.set()

        handles = self._runtime_handles.get(run_id, {})
        if handles.get("chaos"):
            self.chaos.stop({"run_id": run_id}, handles["chaos"])
        if handles.get("load"):
            self.load.stop_load({"run_id": run_id}, handles["load"])

        run = self.storage.get_run(run_id)
        run.status = RunStatus.STOPPED
        run.ended_at = utc_now_iso()
        run.summary["stop_reason"] = reason
        self.storage.update_run(run)

        self._event(
            run_id, "run_stopped", {"requested_by": requested_by, "reason": reason}
        )
        self.notifier.notify("run_stopped", {"run_id": run_id, "reason": reason})
        return run

    def _execute_run(self, run_id: str, exp: ExperimentVersion) -> None:
        run = self.storage.get_run(run_id)
        run.status = RunStatus.RUNNING
        self.storage.update_run(run)

        now = datetime.now(timezone.utc)
        run_start_iso = now.isoformat()

        timeline = exp.spec.get("timeline", {})
        governance = exp.spec.get("governance", {})
        scope = exp.spec.get("scope", {})
        baseline_seconds = int(timeline.get("baseline_seconds", 0))
        warmup_seconds = int(timeline.get("warmup_seconds", 60))
        fault_duration_seconds = int(timeline.get("fault_duration_seconds", 60))
        post_seconds = int(timeline.get("post_recovery_observation_seconds", 120))
        step = int(timeline.get("sampling_interval_seconds", 15))

        guardrails = governance.get("guardrails", {})
        max_fault_duration = int(guardrails.get("max_fault_duration_seconds", 1800))
        if fault_duration_seconds > max_fault_duration:
            raise ValueError(
                "fault_duration_seconds exceeds governance guardrail "
                f"({fault_duration_seconds} > {max_fault_duration})"
            )

        if str(scope.get("environment", "")).lower() == "production":
            if not governance.get("approved_window"):
                raise ValueError("Production run requires governance.approved_window")

        if (
            min(
                baseline_seconds,
                warmup_seconds,
                fault_duration_seconds,
                post_seconds,
                step,
            )
            < 0
        ):
            raise ValueError("Timeline values must be non-negative")
        if step == 0:
            raise ValueError("sampling_interval_seconds must be > 0")

        context = {"run_id": run_id, "experiment_id": exp.experiment_id}
        stop_flag = self._stop_flags[run_id]

        try:
            if exp.spec.get("controlled_campaign", {}).get("enabled") is True:
                from .controlled_experiment import ControlledRun

                ControlledRun(self, run_id, exp.spec).run()
                return
            load_cfg = dict(exp.spec.get("load_profile", {}))
            chaos_cfg = dict(exp.spec.get("chaos_profile", {}))
            obs = exp.spec.get("observability", {})
            metrics_cfg = dict(obs.get("metrics", {}))
            traces_cfg = dict(obs.get("traces", {}))

            # provider "none" = sham/control run: identical schedule and load,
            # no injection. Gives the null distribution for verdicts and RCA.
            sham_run = str(chaos_cfg.get("provider") or "").lower() == "none"

            self.load.validate(load_cfg)
            if not sham_run:
                self.chaos.validate(chaos_cfg)
            self.metrics.validate(metrics_cfg)
            self.traces.validate(traces_cfg)

            self.load.prepare(context, load_cfg)
            if not sham_run:
                self.chaos.prepare(context, chaos_cfg)

            self._event(run_id, "run_started", {"at": run_start_iso})

            # Load runs through ALL phases (baseline → warmup → fault → post)
            # so baseline metrics capture real traffic before the fault.
            total_load = (
                baseline_seconds
                + warmup_seconds
                + fault_duration_seconds
                + post_seconds
            )
            load_cfg["run_time_seconds"] = max(total_load, 1)
            load_handle = self.load.start_load(context, load_cfg)
            self._runtime_handles.setdefault(run_id, {})["load"] = load_handle

            if baseline_seconds > 0:
                self._event(run_id, "baseline_started", {"seconds": baseline_seconds})
                self._sleep_interruptible(stop_flag, baseline_seconds)
                self._event(run_id, "baseline_completed", {})

            warmup_start = utc_now_iso()

            self._event(run_id, "warmup_started", {"seconds": warmup_seconds})
            self._sleep_interruptible(stop_flag, warmup_seconds)
            warmup_end = utc_now_iso()
            self._event(run_id, "warmup_completed", {})

            fault_start = utc_now_iso()
            if sham_run:
                chaos_handle = {
                    "provider": "none",
                    "mode": "sham",
                    "namespace": chaos_cfg.get("namespace"),
                    "injected_at": None,
                }
            else:
                chaos_handle = self.chaos.inject(context, chaos_cfg)
            self._runtime_handles.setdefault(run_id, {})["chaos"] = chaos_handle
            self._event(
                run_id,
                "fault_started",
                {"seconds": fault_duration_seconds, "sham": sham_run},
            )

            self._sleep_interruptible(stop_flag, fault_duration_seconds)

            fault_end = utc_now_iso()
            if not sham_run:
                self.chaos.stop(context, chaos_handle)
            self._event(run_id, "fault_completed", {})

            self._event(
                run_id, "recovery_observation_started", {"seconds": post_seconds}
            )
            self._sleep_interruptible(stop_flag, post_seconds)
            recovery_end = utc_now_iso()

            self.load.stop_load(context, load_handle)
            load_summary = self.load.collect_summary(context, load_handle)
            chaos_artifacts = (
                dict(chaos_handle)
                if sham_run
                else self.chaos.collect_artifacts(context, chaos_handle)
            )
            planned_fault_start, planned_fault_end = fault_start, fault_end
            fault_start, fault_end, fault_window_source = _resolve_fault_window(
                planned_fault_start,
                planned_fault_end,
                chaos_artifacts,
                chaos_cfg.get("fault_duration_seconds"),
            )
            fault_injection_duration = (
                _duration_seconds(fault_start, fault_end)
                if fault_window_source != "planned_phase_fallback"
                else None
            )
            warmup_end = fault_start

            metrics_raw = self.metrics.collect_window(
                context, metrics_cfg, run_start_iso, recovery_end, step
            )
            metrics_summary = self.metrics.summarize(metrics_raw)
            # Collect the full execution window so baseline traces can be
            # compared with fault and recovery traces.
            traces_raw = self.traces.collect_window(
                context, traces_cfg, run_start_iso, recovery_end
            )
            traces_summary = self.traces.summarize(traces_raw)

            metrics_path = self.storage.save_artifact(
                run_id, "metrics_raw", metrics_raw
            )
            traces_path = self.storage.save_artifact(run_id, "traces_raw", traces_raw)
            load_path = self.storage.save_artifact(run_id, "load_summary", load_summary)
            chaos_path = self.storage.save_artifact(
                run_id, "chaos_artifacts", chaos_artifacts
            )

            phases = [
                ("baseline", run_start_iso, warmup_start),
                ("warmup", warmup_start, warmup_end),
                ("fault", fault_start, fault_end),
                ("post", fault_end, recovery_end),
            ]
            features = self._build_run_features(
                spec=exp.spec,
                metrics_raw=metrics_raw,
                traces_raw=traces_raw,
                traces_summary=traces_summary,
                phases=phases,
                fault_end_iso=fault_end,
                recovery_end_iso=recovery_end,
            )

            # ── Ground-truth auto-labelling ─────────────────────────────────
            # Experiments submitted by the eval harness carry their ground
            # truth in the experiment-level tags as ``target:<service>`` and
            # ``fault_category:<category>``. Extract them here — BEFORE the
            # gat_graph build below, which reads features["ground_truth_*"] —
            # so every persisted graph has a stable label that doesn't depend
            # on the eval harness keeping a static chaos_type → fault_category
            # map. Future fault families just need to ship the right tag on
            # the experiment spec.
            exp_meta = (
                exp.spec.get("experiment", {}) if isinstance(exp.spec, dict) else {}
            )
            spec_tags = exp_meta.get("tags") or []
            gt_service_from_spec: Optional[str] = None
            gt_fault_from_spec: Optional[str] = None
            for tag in spec_tags:
                if not isinstance(tag, str):
                    continue
                if tag.startswith("target:") and gt_service_from_spec is None:
                    gt_service_from_spec = tag.split(":", 1)[1].strip() or None
                elif tag.startswith("fault_category:") and gt_fault_from_spec is None:
                    gt_fault_from_spec = tag.split(":", 1)[1].strip() or None
            if gt_service_from_spec:
                features["ground_truth_service"] = gt_service_from_spec
            if gt_fault_from_spec:
                features["ground_truth_fault_category"] = gt_fault_from_spec

            # Phase-scoped service graph delta — optional evidence for WHERE.
            # The payload is intentionally model-facing but safe: it contains
            # only window timestamps, service-to-service deltas and first
            # degraded signals. It never includes chaos target, fault label,
            # experiment tags, or ground truth fields.
            graph_method = getattr(self.traces, "collect_phase_graphs", None)
            if callable(graph_method):
                try:
                    settle_s = int(traces_cfg.get("graph_settle_seconds", 0) or 0)
                    if settle_s > 0:
                        self._event(
                            run_id,
                            "service_graph_delta_waiting",
                            {"seconds": settle_s},
                        )
                        time.sleep(settle_s)
                    phase_payload = [
                        {"name": name, "start": start, "end": end}
                        for name, start, end in phases
                        if name in {"baseline", "fault", "post"}
                    ]
                    service_graph_delta = graph_method(
                        context,
                        traces_cfg,
                        phase_payload,
                    )
                    if isinstance(service_graph_delta, dict) and service_graph_delta:
                        features["service_graph_delta"] = service_graph_delta
                except Exception as exc:  # pragma: no cover - best effort
                    self._event(
                        run_id,
                        "service_graph_delta_failed",
                        {"error": str(exc)[:500]},
                    )

            features_path = self.storage.save_artifact(run_id, "run_features", features)

            # ── Istio edge features + grafo GAT (Opção D + mesh) ───────────
            # Coleta golden metrics por aresta (source→destination) via Istio e
            # constrói o grafo do GAT. Best-effort: sem mesh, edge_source fica
            # "static" e o gate edge_features_present acusa.
            try:
                from .analysis.graph_builder import build_gat_graph
                from .analysis.istio_edges import collect_istio_edges

                metrics_cfg_ns = (obs.get("metrics") or {}).get("namespace") or "sock-shop"
                edge_metrics = collect_istio_edges(
                    self.metrics._client(metrics_cfg),
                    run_start_iso,
                    recovery_end,
                    step,
                    namespace=metrics_cfg_ns,
                )
                if edge_metrics:
                    self.storage.save_artifact(run_id, "istio_edges", edge_metrics)
                    gt_service = features.get("ground_truth_service")
                    gt_fault = features.get("ground_truth_fault_category")
                    gat_graph = build_gat_graph(
                        run_id=run_id,
                        metrics_raw=metrics_raw,
                        baseline_window=(run_start_iso, warmup_start),
                        fault_window=(fault_start, fault_end),
                        ground_truth_service=gt_service,
                        ground_truth_fault_category=gt_fault,
                        is_training=bool(run.summary.get("is_training", True)),
                        edge_metrics=edge_metrics,
                    )
                    self.storage.save_artifact(run_id, "gat_graph", gat_graph)
                    self._event(run_id, "gat_graph_built", {
                        "edge_source": gat_graph.get("edge_source"),
                        "node_count": len(gat_graph.get("nodes") or []),
                        "edge_count": len(gat_graph.get("edges") or []),
                    })
            except Exception as exc:  # pragma: no cover - best effort
                self._event(run_id, "gat_graph_failed", {"error": str(exc)[:500]})

            # ── L2: deterministic, RAG-ready summary + tags ─────────────────
            run_timings_preview = {
                "fault_injection_duration_seconds": fault_injection_duration,
                "experiment_total_duration_seconds": _duration_seconds(
                    run_start_iso, utc_now_iso()
                ),
            }
            try:
                summary_text, tags = build_run_summary_l2(
                    run_id=run_id,
                    experiment_id=exp.experiment_id,
                    features=features,
                    timings=run_timings_preview,
                )
                features["summary_text"] = summary_text
                features["tags"] = tags
                self.storage.save_artifact(
                    run_id,
                    "run_summary_l2",
                    {
                        "summary_text": summary_text,
                        "tags": tags,
                    },
                )
            except Exception as exc:  # pragma: no cover - summary best-effort
                self._event(run_id, "run_summary_l2_failed", {"error": str(exc)})

            # Propagate the train/test flag from RunRecord.summary into the
            # features dict so the storage layer can persist it.
            current_run = self.storage.get_run(run_id)
            features["is_training"] = bool(current_run.summary.get("is_training", True))

            try:
                self.storage.upsert_run_features(
                    run_id,
                    exp.experiment_id,
                    features,
                    experiment_version=exp.version,
                )
            except Exception as exc:  # pragma: no cover - storage best-effort
                self._event(run_id, "run_features_persist_failed", {"error": str(exc)})

            # ── Phase C: embed L2 summary for semantic retrieval ─────────────
            summary_text = features.get("summary_text")
            if summary_text:
                try:
                    # Anonymize service names before embedding so retrieval
                    # measures the *shape* of the incident (cascade, hotspots,
                    # p95 dynamics) rather than the lexical overlap of the
                    # service names. The raw summary_text is still kept for
                    # prompt assembly and human inspection.
                    known_services = list(features.get("affected_services") or [])
                    # Augment with services seen in the dependency map / tags
                    # so even "innocent bystanders" get masked consistently.
                    dep_map = features.get("dependency_map") or {}
                    if isinstance(dep_map, dict):
                        for node in dep_map.get("nodes") or []:
                            if isinstance(node, str):
                                known_services.append(node)
                            elif isinstance(node, dict) and node.get("service"):
                                known_services.append(node["service"])
                    anonymized = anonymize_services(summary_text, known_services)
                    provider = get_embedding_provider()
                    vec = provider.embed(anonymized)
                    self.storage.upsert_run_embedding(
                        run_id, provider.name, provider.dim, vector_to_blob(vec)
                    )
                except Exception as exc:  # pragma: no cover - embedding best-effort
                    self._event(run_id, "run_embedding_failed", {"error": str(exc)})

            run = self.storage.get_run(run_id)
            run.status = RunStatus.COMPLETED
            run.ended_at = utc_now_iso()
            run.verdict = features.get("verdict") or "resilient"
            run.timings = {
                "t_start": run_start_iso,
                "t_load_start": run_start_iso,
                "t_warmup_start": warmup_start,
                "t_warmup_end": warmup_end,
                "t_fault_start": fault_start,
                "t_fault_end": fault_end,
                "t_fault_planned_start": planned_fault_start,
                "t_fault_planned_end": planned_fault_end,
                "fault_window_source": fault_window_source,
                "t_recovery_observed": recovery_end,
                "baseline_seconds": baseline_seconds,
                "warmup_time_seconds": warmup_seconds,
                "fault_injection_duration_seconds": fault_injection_duration,
                "experiment_total_duration_seconds": _duration_seconds(
                    run_start_iso, run.ended_at
                ),
                "post_recovery_observation_seconds": post_seconds,
                "sampling_interval_seconds": step,
                "recovery_time_seconds": features.get("recovery_time_seconds"),
            }
            observation_window = _build_observation_window(
                run_id=run_id,
                experiment_id=exp.experiment_id,
                status=run.status.value,
                timings=run.timings,
                sampling_interval_seconds=step,
            )
            observation_window_path = self.storage.save_artifact(
                run_id, "observation_window", observation_window
            )
            artifact_paths = {
                "metrics_raw": metrics_path,
                "traces_raw": traces_path,
                "load_summary": load_path,
                "chaos_artifacts": chaos_path,
                "run_features": features_path,
                "observation_window": observation_window_path,
            }
            self._persist_rca_evidence_snapshot(
                run_id=run_id,
                context=context,
                observability_config=obs,
                namespace=str(scope.get("namespace", "sock-shop")),
                observation_window=observation_window,
                capture_config=obs.get("rca_evidence_snapshot") or {},
                artifact_paths=artifact_paths,
            )
            observability_summary = build_observability_summary(
                run_id=run_id,
                experiment_id=exp.experiment_id,
                observation_window=observation_window,
                metrics_raw=metrics_raw,
                metrics_summary=metrics_summary,
                traces_raw=traces_raw,
                traces_summary=traces_summary,
                features=features,
                artifact_paths=artifact_paths,
            )
            observability_summary_path = self.storage.save_artifact(
                run_id, "observability_summary", observability_summary
            )
            artifact_paths["observability_summary"] = observability_summary_path
            run.summary = {
                "load": load_summary,
                "metrics": metrics_summary,
                "traces": traces_summary,
                "features": features,
                "artifact_paths": artifact_paths,
                "technical_summary": self._technical_summary(features),
                "executive_summary": self._executive_summary(run.verdict, features),
                "resilience_classification": run.verdict,
                "recommendations": self._recommendations(features),
            }
            self.storage.update_run(run)

            self._event(run_id, "run_completed", {"verdict": run.verdict})
            self.notifier.notify(
                "run_completed", {"run_id": run_id, "verdict": run.verdict}
            )

        except InterruptedError:
            run = self.storage.get_run(run_id)
            run.status = RunStatus.STOPPED
            run.ended_at = utc_now_iso()
            run.summary["stop_reason"] = "interrupt_flag"
            try:
                window = _build_partial_observation_window(
                    run_id=run_id,
                    experiment_id=exp.experiment_id,
                    status=run.status.value,
                    events=self.storage.list_events(run_id),
                    ended_at=run.ended_at,
                    sampling_interval_seconds=step,
                )
                run.summary.setdefault("artifact_paths", {})[
                    "observation_window"
                ] = self.storage.save_artifact(
                    run_id, "observation_window", window
                )
            except Exception as window_exc:  # pragma: no cover - best effort
                self._event(
                    run_id,
                    "observation_window_failed",
                    {"error": str(window_exc)[:500]},
                )
            self.storage.update_run(run)
            self._event(run_id, "run_stopped", {"reason": "interrupt_flag"})
        except Exception as exc:
            import traceback

            # Captura o traceback completo + sub-exceções (ExceptionGroup /
            # TaskGroup escondem a causa real em `str(exc)`). Sem isto, um erro
            # "unhandled errors in a TaskGroup" não diz onde falhou.
            tb = traceback.format_exc()
            sub = []
            for e in getattr(exc, "exceptions", []) or []:
                sub.append("".join(traceback.format_exception(type(e), e, e.__traceback__))[-800:])
            run = self.storage.get_run(run_id)
            run.status = RunStatus.FAILED
            run.ended_at = utc_now_iso()
            run.summary["error"] = str(exc)
            try:
                if (
                    exp.spec.get("controlled_campaign", {}).get("enabled") is True
                    and run.summary.get("artifact_paths", {}).get("observation_window")
                ):
                    window = self.storage.get_artifact(run_id, "observation_window")
                else:
                    window = _build_partial_observation_window(
                        run_id=run_id,
                        experiment_id=exp.experiment_id,
                        status=run.status.value,
                        events=self.storage.list_events(run_id),
                        ended_at=run.ended_at,
                        sampling_interval_seconds=step,
                    )
                run.summary.setdefault("artifact_paths", {})[
                    "observation_window"
                ] = self.storage.save_artifact(
                    run_id, "observation_window", window
                )
            except Exception as window_exc:  # pragma: no cover - best effort
                self._event(
                    run_id,
                    "observation_window_failed",
                    {"error": str(window_exc)[:500]},
                )
            self.storage.update_run(run)
            self._event(
                run_id,
                "run_failed",
                {"error": str(exc), "traceback": tb[-2000:], "sub_exceptions": sub},
            )
            self.notifier.notify("run_failed", {"run_id": run_id, "error": str(exc)})
        finally:
            try:
                self._dispatch_history_migration(run_id, exp)
            finally:
                self._runtime_handles.pop(run_id, None)
                self._stop_flags.pop(run_id, None)

    def _dispatch_history_migration(
        self, run_id: str, experiment: ExperimentVersion
    ) -> None:
        historical = experiment.spec.get("historical_observability") or {}
        dual_write = historical.get("mode") == "dual_write"
        if self.history_migration is None and not dual_write:
            return
        try:
            run = self.storage.get_run(run_id)
            observation_window = self.storage.get_artifact(
                run_id, "observation_window"
            )
            if not isinstance(observation_window, dict):
                observation_window = {}
            log_snapshot = self.storage.get_artifact(
                run_id, "rca_evidence_snapshot"
            )
            if not isinstance(log_snapshot, dict):
                log_snapshot = None
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            self._event(
                run_id,
                "history_migration_input_failed",
                {"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )
            return
        namespace = str(
            (experiment.spec.get("scope") or {}).get("namespace", "sock-shop")
        )
        if dual_write:
            result = {
                "status": "dual_write",
                "campaign_id": historical.get("campaign_id", experiment.experiment_id),
                "mode": "collector_fanout",
            }
        else:
            try:
                if self.history_migration is None:
                    return
                result = self.history_migration.dispatch(
                    run_id=run_id,
                    experiment_id=experiment.experiment_id,
                    status=run.status.value,
                    namespace=namespace,
                    observation_window=observation_window,
                    log_snapshot=log_snapshot,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                result = {
                    "status": "dispatch_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
        if result.get("status") == "disabled":
            return
        try:
            artifact_path = self.storage.save_artifact(
                run_id, "history_migration", result
            )
            run = self.storage.get_run(run_id)
            run.summary.setdefault("artifact_paths", {})[
                "history_migration"
            ] = artifact_path
            run.summary["history_migration"] = result
            self.storage.update_run(run)
            self._event(
                run_id,
                (
                    "history_dual_write_recorded"
                    if dual_write
                    else "history_migration_dispatched"
                ),
                {
                    "status": result.get("status"),
                    "job_name": result.get("job_name"),
                    "error_type": result.get("error_type"),
                },
            )
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
            self._event(
                run_id,
                "history_migration_result_persist_failed",
                {"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )

    def _sleep_interruptible(self, stop_flag: threading.Event, seconds: int) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if stop_flag.is_set():
                raise InterruptedError("Run interrupted")
            if self.kill_switch_path and self.kill_switch_path.exists():
                raise InterruptedError("Global kill-switch activated")
            time.sleep(min(1.0, max(0.0, end - time.time())))

    def _event(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        self.storage.append_event(
            DomainEvent(
                run_id=run_id,
                event_type=event_type,
                ts=utc_now_iso(),
                payload=payload,
            )
        )

    def _classify_verdict(self, spec: Dict[str, Any], features: Dict[str, Any]) -> str:
        """Backwards-compatible thin wrapper around features.verdict."""
        return features.get("verdict") or "resilient"

    # ── L1 features (per-phase SLOs, recovery, affected services) ──────────────
    @staticmethod
    def _slo_thresholds(spec: Dict[str, Any]) -> Dict[str, float]:
        """Extract SLO thresholds from spec.analysis.slo with sane defaults."""
        slo = spec.get("analysis", {}).get("slo", {}) or {}
        out = {
            "error_rate": float(slo.get("error_rate_threshold", 0.05)),
            "latency_p95_ms": float(slo.get("latency_p95_threshold_ms", 800)),
            "recovery_tolerance_pct": float(slo.get("recovery_tolerance_pct", 0.20)),
            "max_recovery_seconds": float(slo.get("max_recovery_seconds", 600)),
        }
        # Optional: threshold = max(absolute, factor × run baseline). Absent →
        # legacy absolute behaviour.
        for key in ("latency_p95_relative_factor", "error_rate_relative_factor"):
            if slo.get(key) is not None:
                out[key] = float(slo[key])
        return out

    # Known fault-type suffixes in descending specificity order so that the
    # most precise match wins (e.g. "pod-delete" before a hypothetical "delete").
    _KNOWN_FAULT_TYPES = (
        "network-latency",
        "network-loss",
        "http-status-code",
        "container-kill",
        "memory-hog",
        "cpu-hog",
        "pod-delete",
    )

    @staticmethod
    def _chaos_type(spec: Dict[str, Any]) -> Optional[str]:
        """Return the fault *type* only, not the full engine name.

        ``chaos_engine`` is the Argo Workflow template name and encodes both
        the target service and the fault type (e.g. ``user-memory-hog``).
        Storing the raw engine name as ``chaos_type`` leaks the injected
        service into the RAG tags, biasing LLM evaluation. We strip the
        service prefix so only the fault type is stored.
        """
        chaos = spec.get("chaos_profile", {}) or {}
        if str(chaos.get("provider") or "").lower() == "none":
            return "none"
        engine = chaos.get("chaos_engine") or ""
        for fault in OrchestratorEngine._KNOWN_FAULT_TYPES:
            if engine.endswith(fault):
                return fault
        # Non-Litmus faults (e.g. Istio VirtualService) carry no chaos_engine.
        # The manifest filename still encodes "<service>-<fault-type>.yaml",
        # so match its stem the same way instead of leaking the raw path
        # (which would bias tags/features with the target service — exactly
        # what this function exists to prevent for chaos_engine).
        stem = Path(chaos.get("manifest_path") or "").stem
        for fault in OrchestratorEngine._KNOWN_FAULT_TYPES:
            if stem.endswith(fault):
                return fault
        return engine or stem or None

    def _build_run_features(
        self,
        spec: Dict[str, Any],
        metrics_raw: Dict[str, Any],
        traces_raw: Dict[str, Any],
        traces_summary: Dict[str, Any],
        phases: list,
        fault_end_iso: str,
        recovery_end_iso: str,
    ) -> Dict[str, Any]:
        slo = self._slo_thresholds(spec)
        per_phase: Dict[str, Any] = {}
        per_phase_method = getattr(self.metrics, "summarize_per_phase", None)
        if callable(per_phase_method):
            try:
                per_phase = per_phase_method(metrics_raw, phases)
            except Exception:
                per_phase = {}

        # ── Per-label hotspots (localisation signal) ────────────────────────
        # ``summarize_per_phase`` discards series labels — the LLM ends up
        # seeing only a namespace-wide aggregate per metric, with no way to
        # tell which service is the actual hotspot. ``per_label_hotspots``
        # preserves the discriminating label (``name`` for RED metrics,
        # ``pod`` for container saturation) and ranks services by
        # fault-vs-baseline deviation, giving the LLM the same view a real
        # SRE has in Grafana.
        hotspots: Dict[str, Any] = {}
        hotspots_method = getattr(self.metrics, "per_label_hotspots", None)
        if callable(hotspots_method):
            try:
                hotspots = hotspots_method(metrics_raw, phases, top_k=3) or {}
            except Exception:
                hotspots = {}

        # ── SLO violations per phase ────────────────────────────────────────
        # Convention: queries with id 'error_rate' and 'latency_p95' map to SLOs.
        slo_violations: list = []
        baseline_means: Dict[str, Optional[float]] = {}

        slo_map = {
            "error_rate": ("error_rate", slo["error_rate"]),
            "latency_p95": ("latency_p95_ms", slo["latency_p95_ms"]),
        }
        relative_factor = {
            "error_rate": slo.get("error_rate_relative_factor"),
            "latency_p95": slo.get("latency_p95_relative_factor"),
        }
        effective_thresholds: Dict[str, Dict[str, Any]] = {}
        for metric_id, (label, threshold) in slo_map.items():
            phase_stats = per_phase.get(metric_id, {})
            base = (
                phase_stats.get("baseline", {}) if isinstance(phase_stats, dict) else {}
            )
            baseline_means[metric_id] = (
                base.get("mean") if isinstance(base, dict) else None
            )
            factor = relative_factor.get(metric_id)
            base_mean = baseline_means[metric_id]
            if factor and base_mean is not None and factor > 0:
                threshold = max(threshold, factor * float(base_mean))
            effective_thresholds[metric_id] = {
                "absolute": slo_map[metric_id][1],
                "relative_factor": factor,
                "baseline_mean": base_mean,
                "effective": threshold,
            }
            slo_map[metric_id] = (label, threshold)
            for phase_name in ("warmup", "fault", "post"):
                s = (
                    phase_stats.get(phase_name, {})
                    if isinstance(phase_stats, dict)
                    else {}
                )
                if not isinstance(s, dict) or s.get("count", 0) == 0:
                    continue
                observed = s.get("p95") if "latency" in metric_id else s.get("max")
                if observed is None:
                    continue
                if observed > threshold:
                    slo_violations.append(
                        {
                            "phase": phase_name,
                            "metric": metric_id,
                            "slo": label,
                            "threshold": threshold,
                            "observed": round(float(observed), 4),
                            "delta_pct": (
                                round(
                                    (float(observed) - threshold) / threshold * 100.0, 1
                                )
                                if threshold
                                else None
                            ),
                        }
                    )

        # ── Recovery time per SLO metric; the run recovers when the SLOWEST
        # violated metric recovers (None if any violated metric never does).
        recovery_method = getattr(self.metrics, "find_recovery_time", None)
        recovery_time: Optional[float] = None
        recovery_metric: Optional[str] = None
        recovery_by_metric: Dict[str, Optional[float]] = {}
        violated_metrics = {v["metric"] for v in slo_violations}
        if callable(recovery_method):
            for metric_id, (_label, threshold) in slo_map.items():
                try:
                    rt = recovery_method(
                        metrics_raw,
                        metric_id,
                        baseline_means.get(metric_id),
                        threshold,
                        fault_end_iso,
                        recovery_end_iso,
                        slo["recovery_tolerance_pct"],
                    )
                except Exception:
                    rt = None
                recovery_by_metric[metric_id] = rt
            considered = violated_metrics or set(slo_map)
            unrecovered = [m for m in considered if recovery_by_metric.get(m) is None]
            if unrecovered:
                recovery_time = None
                recovery_metric = unrecovered[0]
            else:
                recovery_metric = max(
                    considered, key=lambda m: recovery_by_metric.get(m) or 0.0
                )
                recovery_time = recovery_by_metric.get(recovery_metric)

        # ── Trace-derived failure aggregation ───────────────────────────────
        failure_agg: Dict[str, Any] = {}
        agg_method = getattr(self.traces, "aggregate_failures", None)
        if callable(agg_method):
            try:
                failure_agg = agg_method(traces_raw) or {}
            except Exception:
                failure_agg = {}

        # ── Phase F1 — temporal feature engineering ────────────────────────
        temporal_features: Dict[str, Any] = {}
        try:
            temporal_features = compute_temporal_features(
                metrics_raw,
                phases,
                baseline_means=baseline_means,
                slo_thresholds=slo,
                tolerance_pct=slo["recovery_tolerance_pct"],
            )
        except Exception:  # pragma: no cover - best effort
            temporal_features = {}

        # ── Phase F1 — service-level propagation graph from traces ────────
        propagation_graph: Dict[str, Any] = {}
        graph_method = getattr(self.traces, "build_propagation_graph", None)
        if callable(graph_method):
            try:
                propagation_graph = graph_method(traces_raw) or {}
            except Exception:  # pragma: no cover - best effort
                propagation_graph = {}

        # ── Verdict (SLO-driven) ────────────────────────────────────────────
        verdict = "resilient"
        had_violation = (
            bool(slo_violations) or traces_summary.get("error_trace_count", 0) > 0
        )
        if had_violation:
            if recovery_time is None:
                verdict = "degraded_persistent"
            elif recovery_time <= slo["max_recovery_seconds"]:
                verdict = "degraded_recoverable"
            else:
                verdict = "degraded_persistent"

        return {
            "verdict": verdict,
            "chaos_type": self._chaos_type(spec),
            "slo_thresholds": slo,
            "slo_effective_thresholds": effective_thresholds,
            "phase_metrics": per_phase,
            "metric_hotspots": hotspots,
            "slo_violations": slo_violations,
            "recovery_time_seconds": recovery_time,
            "recovery_reference_metric": recovery_metric,
            "recovery_by_metric": recovery_by_metric,
            "affected_services": failure_agg.get("affected_services", []),
            "top_failure_signatures": failure_agg.get("top_failure_signatures", []),
            "rca_hypotheses": failure_agg.get("rca_hypotheses", []),
            "trace_summary": {
                "trace_count": traces_summary.get("trace_count", 0),
                "error_trace_count": traces_summary.get("error_trace_count", 0),
                "duration_ms": traces_summary.get("duration_ms"),
            },
            "temporal_features": temporal_features,
            "propagation_graph": propagation_graph,
        }

    def _technical_summary(self, features: Dict[str, Any]) -> str:
        viol = features.get("slo_violations") or []
        rt = features.get("recovery_time_seconds")
        services = ", ".join(features.get("affected_services") or []) or "none"
        rt_text = f"{rt:.0f}s" if isinstance(rt, (int, float)) else "not recovered"
        return (
            f"SLO violations: {len(viol)}. "
            f"Recovery: {rt_text}. "
            f"Affected services: {services}. "
            f"Failure signatures: {len(features.get('top_failure_signatures') or [])}."
        )

    def _executive_summary(
        self, verdict: Optional[str], features: Dict[str, Any]
    ) -> str:
        viol = features.get("slo_violations") or []
        rt = features.get("recovery_time_seconds")
        services = features.get("affected_services") or []
        if verdict == "resilient":
            return "System remained within SLOs during fault injection — resilient behavior confirmed."
        primary = services[0] if services else "service(s)"
        rt_text = (
            f"recovered in {rt:.0f}s"
            if isinstance(rt, (int, float))
            else "did not recover within window"
        )
        return (
            f"Verdict: {verdict}. {len(viol)} SLO violation(s) detected; "
            f"{primary} most affected; system {rt_text}."
        )

    def _recommendations(self, features: Dict[str, Any]) -> list[str]:
        recs: list[str] = []
        for v in features.get("slo_violations") or []:
            recs.append(
                f"[{v['phase']}] {v['metric']} exceeded SLO "
                f"({v['observed']} > {v['threshold']}) — review retries, timeouts and circuit breakers."
            )
        for sig in (features.get("top_failure_signatures") or [])[:3]:
            recs.append(
                f"Recurring failure '{sig.get('signature')}' on {sig.get('affected_service') or 'unknown'}: "
                f"{sig.get('occurrence_count')} occurrence(s); inspect dependency '{sig.get('endpoint') or 'n/a'}'."
            )
        for rca in (features.get("rca_hypotheses") or [])[:2]:
            if rca.get("confidence") == "HIGH":
                recs.append(f"High-confidence RCA: {rca.get('hypothesis')}")
        if not recs:
            recs.append(
                "No SLO violation detected — consider increasing fault intensity or duration."
            )
        return recs
