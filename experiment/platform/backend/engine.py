from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .domain import DomainEvent, ExperimentVersion, RunRecord, RunStatus, utc_now_iso
from .analysis import (
    build_run_summary_l2,
    compute_temporal_features,
    get_embedding_provider,
    vector_to_blob,
    anonymize_services,
)
from .ports import (
    ChaosProviderPort,
    LoadProviderPort,
    MetricsProviderPort,
    NotificationPort,
    StoragePort,
    TraceProviderPort,
)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration_seconds(start_iso: str, end_iso: str) -> float:
    return (_parse_iso(end_iso) - _parse_iso(start_iso)).total_seconds()


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
    ):
        self.storage = storage
        self.chaos = chaos
        self.load = load
        self.metrics = metrics
        self.traces = traces
        self.notifier = notifier
        self.kill_switch_path = kill_switch_path

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
        initial_status = RunStatus.PENDING_APPROVAL if requires_approval and not approved_by else RunStatus.PENDING

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
        run = self.storage.create_run(run, idempotency_key=idempotency_key)

        self._event(run.run_id, "run_created", {"status": run.status.value, "initiated_by": initiated_by})

        if run.status == RunStatus.PENDING_APPROVAL:
            self.notifier.notify("approval_required", {"run_id": run.run_id, "experiment_id": exp.experiment_id})
            return run

        stop_flag = threading.Event()
        self._stop_flags[run.run_id] = stop_flag
        t = threading.Thread(target=self._execute_run, args=(run.run_id, exp), daemon=True)
        t.start()
        return run

    def approve_run(self, run_id: str, approved_by: str) -> RunRecord:
        run = self.storage.get_run(run_id)
        if run.status != RunStatus.PENDING_APPROVAL:
            return run

        run.status = RunStatus.PENDING
        self.storage.update_run(run)
        exp = self.storage.get_experiment(run.experiment_id, run.experiment_version)

        stop_flag = threading.Event()
        self._stop_flags[run.run_id] = stop_flag
        t = threading.Thread(target=self._execute_run, args=(run.run_id, exp), daemon=True)
        t.start()

        self._event(run_id, "run_approved", {"approved_by": approved_by})
        return self.storage.get_run(run_id)

    def stop_run(self, run_id: str, requested_by: str, reason: str = "manual_stop") -> RunRecord:
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

        self._event(run_id, "run_stopped", {"requested_by": requested_by, "reason": reason})
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
                raise ValueError(
                    "Production run requires governance.approved_window"
                )

        if min(baseline_seconds, warmup_seconds, fault_duration_seconds, post_seconds, step) < 0:
            raise ValueError("Timeline values must be non-negative")
        if step == 0:
            raise ValueError("sampling_interval_seconds must be > 0")

        context = {"run_id": run_id, "experiment_id": exp.experiment_id}
        stop_flag = self._stop_flags[run_id]

        try:
            load_cfg = dict(exp.spec.get("load_profile", {}))
            chaos_cfg = dict(exp.spec.get("chaos_profile", {}))
            obs = exp.spec.get("observability", {})
            metrics_cfg = dict(obs.get("metrics", {}))
            traces_cfg = dict(obs.get("traces", {}))

            self.load.validate(load_cfg)
            self.chaos.validate(chaos_cfg)
            self.metrics.validate(metrics_cfg)
            self.traces.validate(traces_cfg)

            self.load.prepare(context, load_cfg)
            self.chaos.prepare(context, chaos_cfg)

            self._event(run_id, "run_started", {"at": run_start_iso})

            # Load runs through ALL phases (baseline → warmup → fault → post)
            # so baseline metrics capture real traffic before the fault.
            total_load = baseline_seconds + warmup_seconds + fault_duration_seconds + post_seconds
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
            chaos_handle = self.chaos.inject(context, chaos_cfg)
            self._runtime_handles.setdefault(run_id, {})["chaos"] = chaos_handle
            self._event(run_id, "fault_started", {"seconds": fault_duration_seconds})

            self._sleep_interruptible(stop_flag, fault_duration_seconds)

            fault_end = utc_now_iso()
            self.chaos.stop(context, chaos_handle)
            self._event(run_id, "fault_completed", {})

            self._event(run_id, "recovery_observation_started", {"seconds": post_seconds})
            self._sleep_interruptible(stop_flag, post_seconds)
            recovery_end = utc_now_iso()

            self.load.stop_load(context, load_handle)
            load_summary = self.load.collect_summary(context, load_handle)
            chaos_artifacts = self.chaos.collect_artifacts(context, chaos_handle)

            metrics_raw = self.metrics.collect_window(context, metrics_cfg, run_start_iso, recovery_end, step)
            metrics_summary = self.metrics.summarize(metrics_raw)
            traces_raw = self.traces.collect_window(context, traces_cfg, fault_start, recovery_end)
            traces_summary = self.traces.summarize(traces_raw)

            metrics_path = self.storage.save_artifact(run_id, "metrics_raw", metrics_raw)
            traces_path = self.storage.save_artifact(run_id, "traces_raw", traces_raw)
            load_path = self.storage.save_artifact(run_id, "load_summary", load_summary)
            chaos_path = self.storage.save_artifact(run_id, "chaos_artifacts", chaos_artifacts)

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
            features_path = self.storage.save_artifact(run_id, "run_features", features)

            # ── L2: deterministic, RAG-ready summary + tags ─────────────────
            run_timings_preview = {
                "fault_injection_duration_seconds": fault_duration_seconds,
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
                self.storage.save_artifact(run_id, "run_summary_l2", {
                    "summary_text": summary_text,
                    "tags": tags,
                })
            except Exception as exc:  # pragma: no cover - summary best-effort
                self._event(run_id, "run_summary_l2_failed", {"error": str(exc)})

            # Propagate the train/test flag from RunRecord.summary into the
            # features dict so the storage layer can persist it.
            current_run = self.storage.get_run(run_id)
            features["is_training"] = bool(
                current_run.summary.get("is_training", True)
            )

            # ── Ground-truth auto-labelling ─────────────────────────────────
            # Experiments submitted by the eval harness carry their ground
            # truth in the experiment-level tags as ``target:<service>`` and
            # ``fault_category:<category>``. Extract them here so every
            # persisted run row has a stable label that doesn't depend on the
            # eval harness keeping a static chaos_type → fault_category map.
            # Future fault families (config-error, network-degradation, etc.)
            # just need to ship the right tag on the experiment spec.
            exp_meta = exp.spec.get("experiment", {}) if isinstance(exp.spec, dict) else {}
            spec_tags = exp_meta.get("tags") or []
            gt_service: Optional[str] = None
            gt_fault: Optional[str] = None
            for tag in spec_tags:
                if not isinstance(tag, str):
                    continue
                if tag.startswith("target:") and gt_service is None:
                    gt_service = tag.split(":", 1)[1].strip() or None
                elif tag.startswith("fault_category:") and gt_fault is None:
                    gt_fault = tag.split(":", 1)[1].strip() or None
            if gt_service:
                features["ground_truth_service"] = gt_service
            if gt_fault:
                features["ground_truth_fault_category"] = gt_fault

            try:
                self.storage.upsert_run_features(run_id, exp.experiment_id, features)
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
                "t_recovery_observed": recovery_end,
                "baseline_seconds": baseline_seconds,
                "warmup_time_seconds": warmup_seconds,
                "fault_injection_duration_seconds": fault_duration_seconds,
                "experiment_total_duration_seconds": _duration_seconds(run_start_iso, run.ended_at),
                "post_recovery_observation_seconds": post_seconds,
                "recovery_time_seconds": features.get("recovery_time_seconds"),
            }
            run.summary = {
                "load": load_summary,
                "metrics": metrics_summary,
                "traces": traces_summary,
                "features": features,
                "artifact_paths": {
                    "metrics_raw": metrics_path,
                    "traces_raw": traces_path,
                    "load_summary": load_path,
                    "chaos_artifacts": chaos_path,
                    "run_features": features_path,
                },
                "technical_summary": self._technical_summary(features),
                "executive_summary": self._executive_summary(run.verdict, features),
                "resilience_classification": run.verdict,
                "recommendations": self._recommendations(features),
            }
            self.storage.update_run(run)

            self._event(run_id, "run_completed", {"verdict": run.verdict})
            self.notifier.notify("run_completed", {"run_id": run_id, "verdict": run.verdict})

        except InterruptedError:
            run = self.storage.get_run(run_id)
            run.status = RunStatus.STOPPED
            run.ended_at = utc_now_iso()
            run.summary["stop_reason"] = "interrupt_flag"
            self.storage.update_run(run)
            self._event(run_id, "run_stopped", {"reason": "interrupt_flag"})
        except Exception as exc:
            run = self.storage.get_run(run_id)
            run.status = RunStatus.FAILED
            run.ended_at = utc_now_iso()
            run.summary["error"] = str(exc)
            self.storage.update_run(run)
            self._event(run_id, "run_failed", {"error": str(exc)})
            self.notifier.notify("run_failed", {"run_id": run_id, "error": str(exc)})
        finally:
            self._runtime_handles.pop(run_id, None)
            self._stop_flags.pop(run_id, None)

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
        return {
            "error_rate": float(slo.get("error_rate_threshold", 0.05)),
            "latency_p95_ms": float(slo.get("latency_p95_threshold_ms", 800)),
            "recovery_tolerance_pct": float(slo.get("recovery_tolerance_pct", 0.20)),
            "max_recovery_seconds": float(slo.get("max_recovery_seconds", 600)),
        }

    # Known fault-type suffixes in descending specificity order so that the
    # most precise match wins (e.g. "pod-delete" before a hypothetical "delete").
    _KNOWN_FAULT_TYPES = ("memory-hog", "cpu-hog", "pod-delete")

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
        engine = chaos.get("chaos_engine") or ""
        for fault in OrchestratorEngine._KNOWN_FAULT_TYPES:
            if engine.endswith(fault):
                return fault
        # Fallback: return whatever is available (manifest_path for non-Litmus)
        return engine or chaos.get("manifest_path") or None

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
        for metric_id, (label, threshold) in slo_map.items():
            phase_stats = per_phase.get(metric_id, {})
            base = phase_stats.get("baseline", {}) if isinstance(phase_stats, dict) else {}
            baseline_means[metric_id] = base.get("mean") if isinstance(base, dict) else None
            for phase_name in ("warmup", "fault", "post"):
                s = phase_stats.get(phase_name, {}) if isinstance(phase_stats, dict) else {}
                if not isinstance(s, dict) or s.get("count", 0) == 0:
                    continue
                observed = s.get("p95") if "latency" in metric_id else s.get("max")
                if observed is None:
                    continue
                if observed > threshold:
                    slo_violations.append({
                        "phase": phase_name,
                        "metric": metric_id,
                        "slo": label,
                        "threshold": threshold,
                        "observed": round(float(observed), 4),
                        "delta_pct": round((float(observed) - threshold) / threshold * 100.0, 1) if threshold else None,
                    })

        # ── Recovery time (use error_rate first, fall back to latency_p95) ──
        recovery_method = getattr(self.metrics, "find_recovery_time", None)
        recovery_time: Optional[float] = None
        recovery_metric: Optional[str] = None
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
                if rt is not None:
                    recovery_time = rt
                    recovery_metric = metric_id
                    break

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
        had_violation = bool(slo_violations) or traces_summary.get("error_trace_count", 0) > 0
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
            "phase_metrics": per_phase,
            "metric_hotspots": hotspots,
            "slo_violations": slo_violations,
            "recovery_time_seconds": recovery_time,
            "recovery_reference_metric": recovery_metric,
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

    def _executive_summary(self, verdict: Optional[str], features: Dict[str, Any]) -> str:
        viol = features.get("slo_violations") or []
        rt = features.get("recovery_time_seconds")
        services = features.get("affected_services") or []
        if verdict == "resilient":
            return "System remained within SLOs during fault injection — resilient behavior confirmed."
        primary = services[0] if services else "service(s)"
        rt_text = f"recovered in {rt:.0f}s" if isinstance(rt, (int, float)) else "did not recover within window"
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
            recs.append("No SLO violation detected — consider increasing fault intensity or duration.")
        return recs
