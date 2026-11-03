"""FastAPI application factory and HTTP routing layer.

Exposes :func:`create_app`, which wires the orchestrator engine, storage
and provider plugins into the REST API used to drive chaos experiments.
This is the inbound adapter of the hexagonal architecture: it translates
HTTP requests into domain operations and serialises domain state back out.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import sqlite3
import time
from uuid import uuid4
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from .adapters.noop_notifier import NoopNotificationAdapter
from .analysis import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_LLM_URL,
    LLMClientError,
    assemble_prompt,
    blob_to_vector,
    call_llm,
    get_embedding_provider,
    list_system_cards,
    llm_health,
    localize_service,
    parse_verdict_response,
    rank_by_embedding,
    rank_hybrid,
    rank_similar_runs,
    render_system_card,
    system_card_meta,
    validate_fault_category,
    vector_to_blob,
)
from .engine import (
    OrchestratorEngine,
    _build_observation_window,
    _build_partial_observation_window,
)
from .controlled_experiment import CONTROLLED_CAMPAIGN_PROTOCOL_VERSION
from .domain import DomainEvent
from .analysis.mcp_investigator import investigate_metric_window
from .agent_console import (
    answer_query,
    answer_query_mcp,
    answer_run_rca_mcp,
    rca_reference_phases,
)
from .plugins.mcp_client import CompositeMCPToolClient, MCPToolClient
from .models import (
    ApproveRunRequest,
    CreateExperimentRequest,
    ManualConclusionRequest,
    OperatorLabelRequest,
    StartRunRequest,
    StopRunRequest,
    TelemetryChatRequest,
)
from .observability import (
    GenAI,
    capture_content,
    configure_llm_io_audit,
    get_tracer,
    record_llm_io,
    record_llm_metrics,
    record_override,
    setup_telemetry,
)
from .plugins.litmus_plugin import LitmusChaosPlugin
from .plugins.locust_plugin import LocustLoadPlugin
from .plugins.mcp_evidence_snapshot_plugin import (
    MCPRcaEvidenceSnapshotPlugin,
)
from .plugins.mcp_metrics_plugin import MCPPrometheusMetricsPlugin
from .plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from .history_job import HistoryMigrationDispatcher
from .history_config import DEFAULT_HISTORICAL_MCP_SSE_URL
from .history_archive import (
    HistoryArchiveError,
    HistoryArchiveNotFound,
    HistoryArchiveReader,
)
from .history_stream import validate_history_stream
from .security import require_roles
from .rca_skill import SkillRCAError, answer_skill_rca
from .rca_options import RCABenchmarkOptions
from .rca_knowledge import (
    VERSION as KNOWLEDGE_VERSION,
    FreezeRequest,
    KnowledgeOptions,
    KnowledgeStore,
    digest as knowledge_digest,
    historical_evidence_sources_valid,
    investigate as investigate_knowledge,
    load_skill,
)
from .analysis.llm_client import call_llm_tool_messages
from .multiagent_rca import (
    InvestigationBudget,
    InvestigationContext,
    InvestigationPlan,
    PROMPT_VERSION as MULTIAGENT_PROMPT_VERSION,
    STRATEGY_VERSION as MULTIAGENT_STRATEGY_VERSION,
    answer_multiagent_rca,
    build_investigation_plan,
)
from .analysis.system_card import SystemCardError, load_system_card
from .storage.sqlite_storage import SqliteStorage


def _select_telemetry_windows(
    observation_window: dict, scope: str
) -> tuple[dict, Optional[dict]]:
    if scope == "run":
        return observation_window["window"], None
    if scope != "fault":
        raise ValueError(f"unsupported telemetry window scope: {scope!r}")

    phases = observation_window.get("phases") or {}
    fault_window = phases.get("fault")
    if not isinstance(fault_window, dict) or not fault_window.get("start") or not fault_window.get("end"):
        raise ValueError("telemetry chat requires a fault phase window")
    baseline_window = phases.get("baseline")
    return fault_window, baseline_window if isinstance(baseline_window, dict) else None


def _build_multiagent_context(
    run_id: str,
    namespace: str,
    window: dict,
    possible_fault_types: list[str] | None = None,
) -> InvestigationContext:
    if not isinstance(namespace, str) or not re.fullmatch(
        r"[a-z0-9]+(?:[-.][a-z0-9]+)*", namespace
    ):
        raise ValueError("Invalid namespace for multiagent RCA")
    card = load_system_card(namespace)
    return InvestigationContext(
        run_id=run_id,
        namespace=namespace,
        known_services=sorted((card.get("services") or {}).keys()),
        allowed_categories=list(card.get("allowed_fault_categories") or []),
        possible_fault_types=possible_fault_types or [],
        phases={
            name: phase
            for name, phase in (window.get("phases") or {}).items()
            if name in {"baseline", "fault", "post"}
        },
        phase_sources=window.get("phase_sources") or {},
        partial=window.get("partial", False),
    )


def _mcp_client_timeout_seconds(
    metrics: dict[str, Any],
    traces: dict[str, Any],
    historical: dict[str, Any],
) -> int:
    timeouts = [
        int(metrics.get("timeout_seconds", 10)),
        int(traces.get("timeout_seconds", 20)),
    ]
    timeout = historical.get("timeout_seconds", 30)
    if type(timeout) is not int or timeout < 1:
        raise HTTPException(
            status_code=409,
            detail="historical timeout_seconds must be a positive integer",
        )
    timeouts.append(timeout)
    return max(timeouts)


def create_app() -> FastAPI:
    backend_dir = Path(__file__).resolve().parent
    platform_dir = backend_dir.parent
    data_dir = platform_dir / "data"
    frontend_dir = platform_dir / "frontend"
    kill_switch_path = data_dir / "KILL_SWITCH"

    storage = SqliteStorage(
        db_path=data_dir / "platform.db",
        object_store_root=data_dir / "object_store",
    )
    history_archive = HistoryArchiveReader.from_environment()

    engine = OrchestratorEngine(
        storage=storage,
        chaos=LitmusChaosPlugin(),
        load=LocustLoadPlugin(),
        metrics=MCPPrometheusMetricsPlugin(),
        traces=MCPTempoTracesPlugin(),
        notifier=NoopNotificationAdapter(),
        kill_switch_path=kill_switch_path,
        evidence_snapshot=MCPRcaEvidenceSnapshotPlugin(),
        history_migration=HistoryMigrationDispatcher.from_environment(),
    )

    app = FastAPI(title="Chaos Platform", version="1.0.0")

    # OpenTelemetry (AgentOps): traces/metrics for the RCA decision pipeline.
    setup_telemetry(app)
    configure_llm_io_audit(str(data_dir / "llm_io_audit.jsonl"))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/controlled-campaign/capabilities")
    def get_controlled_campaign_capabilities() -> dict:
        return {"protocol_version": CONTROLLED_CAMPAIGN_PROTOCOL_VERSION}

    @app.post("/api/experiments")
    def create_experiment(
        req: CreateExperimentRequest,
        _: str = Depends(require_roles({"chaos_engineer", "admin"})),
    ) -> dict:
        try:
            version = storage.create_experiment(req.initiated_by, req.spec)
            return {
                "experiment_id": version.experiment_id,
                "version": version.version,
                "schema_version": version.schema_version,
                "created_at": version.created_at,
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/experiments")
    def list_experiments() -> list[dict]:
        return storage.list_experiments()

    @app.get("/api/multiagent-rca/strategy")
    def get_multiagent_rca_strategy() -> dict:
        return {
            "strategy_version": MULTIAGENT_STRATEGY_VERSION,
            "prompt_version": MULTIAGENT_PROMPT_VERSION,
        }

    @app.get("/api/skill-rag/strategy")
    def get_skill_rag_strategy() -> dict:
        return {
            "version": KNOWLEDGE_VERSION,
            "modes": [
                "annotate", "control", "rag", "exploratory_control",
                "exploratory_rag",
            ],
        }

    def knowledge_store(corpus_id: str) -> KnowledgeStore:
        return KnowledgeStore(storage.object_store_root / ".rca_knowledge", corpus_id)

    def read_knowledge_base(corpus_id: str) -> tuple[dict, str] | None:
        try:
            store = knowledge_store(corpus_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        base_paths = [
            store.directory / "base.json",
            store.directory / "exploratory_base.json",
        ]
        installed = [path for path in base_paths if path.is_file()]
        if not installed:
            return None
        if len(installed) != 1:
            raise HTTPException(
                status_code=409,
                detail=f"Knowledge corpus {corpus_id} has conflicting frozen bases",
            )

        path = installed[0]
        try:
            base = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot read knowledge corpus {corpus_id}: {exc}",
            ) from exc
        if not isinstance(base, dict):
            raise HTTPException(
                status_code=409,
                detail=f"Knowledge corpus {corpus_id} is malformed",
            )
        payload = {key: value for key, value in base.items() if key != "sha256"}
        pairs = base.get("pairs")
        try:
            checksum_matches = base.get("sha256") == knowledge_digest(payload)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Knowledge corpus {corpus_id} failed integrity validation",
            ) from exc
        if (
            not checksum_matches
            or not isinstance(pairs, list)
            or not isinstance(base.get("training_run_ids"), list)
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Knowledge corpus {corpus_id} failed integrity validation",
            )
        if any(
            not isinstance(pair, dict)
            or not isinstance(pair.get("a"), dict)
            or not isinstance(pair.get("b"), dict)
            or not isinstance(pair.get("pair_id"), str)
            or not isinstance(pair.get("source_run_id"), str)
            for pair in pairs
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Knowledge corpus {corpus_id} contains malformed A/B pairs",
            )
        mode = "exploratory" if path.name == "exploratory_base.json" else "validated"
        return base, mode

    @app.get("/api/skill-rag/corpora")
    def list_skill_knowledge_corpora(
        _: str = Depends(require_roles({"chaos_engineer", "admin"})),
    ) -> list[dict]:
        root = storage.object_store_root / ".rca_knowledge"
        if not root.is_dir():
            return []

        corpora = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", directory.name)
            ):
                continue
            stored = read_knowledge_base(directory.name)
            if stored is None:
                continue
            base, mode = stored
            pairs = base["pairs"]
            corpora.append(
                {
                    "corpus_id": directory.name,
                    "mode": mode,
                    "version": base.get("version"),
                    "sha256": base["sha256"],
                    "source_model": base.get("model"),
                    "pair_count": len(pairs),
                    "effect_validated_count": sum(
                        pair.get("effect_validated") is True for pair in pairs
                    ),
                    "training_run_count": len(base["training_run_ids"]),
                }
            )
        return corpora

    @app.get("/api/skill-rag/{corpus_id}/pairs")
    def get_skill_knowledge_pairs(
        corpus_id: str,
        _: str = Depends(require_roles({"chaos_engineer", "admin"})),
    ) -> dict:
        stored = read_knowledge_base(corpus_id)
        if stored is None:
            raise HTTPException(
                status_code=404, detail=f"No frozen knowledge base for {corpus_id}"
            )
        base, mode = stored
        pair_fields = (
            "pair_id",
            "source_run_id",
            "a",
            "b",
            "labels",
            "guide_status",
            "causal_status",
            "effect_validated",
            "review_status",
            "sha256",
        )
        return {
            "corpus_id": corpus_id,
            "mode": mode,
            "version": base.get("version"),
            "sha256": base["sha256"],
            "source_model": base.get("model"),
            "source_skill_sha256": base.get("skill_sha256"),
            "a_text_version": base.get("a_text_version"),
            "pair_count": len(base["pairs"]),
            "pairs": [
                {key: pair[key] for key in pair_fields if key in pair}
                for pair in base["pairs"]
            ],
        }

    @app.post("/api/skill-rag/{corpus_id}/freeze")
    def freeze_skill_knowledge(
        corpus_id: str, request: FreezeRequest,
        _: str = Depends(require_roles({"chaos_engineer", "admin"})),
    ) -> dict:
        try:
            store = knowledge_store(corpus_id)
            with store.locked():
                for source_id in request.training_run_ids:
                    source = storage.get_run(source_id)
                    spec = storage.get_experiment(
                        source.experiment_id, source.experiment_version
                    ).spec
                    if spec.get("knowledge_campaign") != {
                        "corpus_id": corpus_id, "split": "train"
                    }:
                        raise ValueError("freeze accepts only this corpus's training runs")
                base = store.freeze(request.training_run_ids)
            return {
                "version": base["version"], "sha256": base["sha256"],
                "annotation_count": base["annotation_count"],
                "eligible_count": len(base["pairs"]),
                "excluded_count": base["excluded_count"],
            }
        except (ValueError, KeyError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/skill-rag/{corpus_id}/exploratory-base")
    def get_exploratory_skill_base(
        corpus_id: str,
        expected_skill_sha256: str | None = None,
        allow_legacy_skill_hash: bool = False,
        expected_model: str | None = None,
        allow_legacy_model_mismatch: bool = False,
        _: str = Depends(require_roles({"chaos_engineer", "admin"})),
    ) -> dict:
        try:
            if allow_legacy_skill_hash and not expected_skill_sha256:
                raise ValueError(
                    "legacy Skill compatibility requires an expected source hash"
                )
            if allow_legacy_model_mismatch and not expected_model:
                raise ValueError(
                    "legacy model compatibility requires a pinned source model"
                )
            base = knowledge_store(corpus_id).load_exploratory_base(
                expected_skill_sha256=expected_skill_sha256,
                expected_model=expected_model,
                allow_legacy_model_mismatch=allow_legacy_model_mismatch,
            )
            _, _, current_skill_sha256 = load_skill()
            if (
                not allow_legacy_skill_hash
                and base["skill_sha256"] != current_skill_sha256
            ):
                raise ValueError(
                    "exploratory base source Skill hash mismatch"
                )
            return {
                "version": base["version"],
                "mode": base["mode"],
                "sha256": base["sha256"],
                "candidate_count": len(base["pairs"]),
                "source_run_ids": base["training_run_ids"],
                "source_skill_sha256": base["skill_sha256"],
                "current_skill_sha256": current_skill_sha256,
                "skill_hashes_match": (
                    base["skill_sha256"] == current_skill_sha256
                ),
                "legacy_skill_hash_allowed": allow_legacy_skill_hash,
                "source_model": base["model"],
                "current_model": DEFAULT_LLM_MODEL,
                "models_match": base["model"] == DEFAULT_LLM_MODEL,
                "legacy_model_mismatch_allowed": allow_legacy_model_mismatch,
            }
        except (ValueError, KeyError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/skill-rag")
    def run_skill_knowledge(
        run_id: str, options: KnowledgeOptions,
        _: str = Depends(require_roles({"chaos_engineer", "admin"})),
    ) -> dict:
        window = get_observation_window(run_id)
        try:
            run = storage.get_run(run_id)
            spec = storage.get_experiment(run.experiment_id, run.experiment_version).spec
            expected_split = "train" if options.mode == "annotate" else "test"
            if spec.get("knowledge_campaign") != {
                "corpus_id": options.corpus_id, "split": expected_split
            }:
                raise ValueError("knowledge mode does not match the run's pinned split/corpus")
            store = knowledge_store(options.corpus_id)
            experimental_suffix = (
                "_include_unvalidated_experimental"
                if options.include_unvalidated_experimental
                else ""
            )
            key = (
                f"skill_knowledge_{options.corpus_id}_{options.mode}"
                f"{experimental_suffix}"
            )
            fingerprint = knowledge_digest(options.model_dump())
            with store.locked():
                previous = storage.get_artifact(run_id, key)
                if previous and previous.get("status") != "error":
                    if previous["options_sha256"] != fingerprint:
                        raise ValueError("completed knowledge attempt used different options")
                    history = spec.get("historical_observability") or {}
                    history_cache_valid = (
                        previous.get("observability_source") == "history"
                        and historical_evidence_sources_valid(
                            previous.get("evidence")
                        )
                    )
                    if history_cache_valid:
                        return {**previous, "cached": True}
                if options.mode == "exploratory_rag":
                    store.load_exploratory_base(
                        expected_skill_sha256=(
                            options.opensearch_source_skill_sha256
                            if options.retrieval_backend == "opensearch"
                            else None
                        ),
                        expected_model=(
                            options.opensearch_source_model
                            if options.retrieval_backend == "opensearch"
                            else None
                        ),
                        allow_legacy_model_mismatch=(
                            options.allow_legacy_model_mismatch
                            if options.retrieval_backend == "opensearch"
                            else False
                        ),
                    )
                elif options.mode not in {"annotate", "exploratory_control"}:
                    store.load_base()
                controlled = spec.get("controlled_campaign") or {}
                labels = (
                    {"service": controlled["service"], "fault_type": controlled["fault"]}
                    if options.mode == "annotate" else None
                )
                observability = spec.get("observability") or {}
                metrics = observability.get("metrics") or {}
                traces = observability.get("traces") or {}
                historical = spec.get("historical_observability") or {}
                historical_url = (
                    historical.get("mcp_sse_url")
                    or DEFAULT_HISTORICAL_MCP_SSE_URL
                )
                gates = run.summary.get("controlled_gates") or {}
                effect = (
                    gates.get("intervention_confirmed_on_target") is True
                    and (gates.get("mechanism_observed") or {}).get("passed") is True
                )
                result = investigate_knowledge(
                    client=MCPToolClient(
                        str(historical_url),
                        _mcp_client_timeout_seconds(metrics, traces, historical),
                    ),
                    phases=window["phases"],
                    namespace=str(spec.get("scope", {}).get("namespace", "sock-shop")),
                    window_metadata={
                        name: window[name] for name in (
                            "phase_sources", "partial", "sampling_interval_seconds"
                        ) if name in window
                    },
                    options=options, run_id=run_id, store=store,
                    llm=call_llm_tool_messages, labels=labels, effect_validated=effect,
                    evidence_source="history",
                )
                result.update(
                    run_id=run_id, attempt_id=uuid4().hex,
                    options_sha256=fingerprint, model=DEFAULT_LLM_MODEL,
                    observability_source="history",
                )
                path = storage.save_artifact(
                    run_id, f"{key}_{result['attempt_id']}", result
                )
                result["artifact_path"] = path
                storage.save_artifact(run_id, key, result)
            return result
        except (ValueError, KeyError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, version: Optional[int] = None) -> dict:
        try:
            exp = storage.get_experiment(experiment_id, version)
            return {
                "experiment_id": exp.experiment_id,
                "version": exp.version,
                "schema_version": exp.schema_version,
                "created_at": exp.created_at,
                "created_by": exp.created_by,
                "spec": exp.spec,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/start")
    def start_run(
        req: StartRunRequest,
        _: str = Depends(require_roles({"operator", "chaos_engineer", "admin"})),
    ) -> dict:
        try:
            exp = storage.get_experiment(req.experiment_id, req.version)
            run = engine.start_run(
                exp=exp,
                initiated_by=req.initiated_by,
                idempotency_key=req.idempotency_key,
                approved_by=req.approved_by,
                is_training=req.is_training,
            )
            return {
                "run_id": run.run_id,
                "status": run.status.value,
                "experiment_id": run.experiment_id,
                "experiment_version": run.experiment_version,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/approve")
    def approve_run(
        run_id: str,
        req: ApproveRunRequest,
        _: str = Depends(require_roles({"admin"})),
    ) -> dict:
        try:
            run = engine.approve_run(run_id, req.approved_by)
            return {"run_id": run.run_id, "status": run.status.value}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(
        run_id: str,
        req: StopRunRequest,
        _: str = Depends(require_roles({"operator", "chaos_engineer", "admin"})),
    ) -> dict:
        try:
            run = engine.stop_run(run_id, req.requested_by, req.reason)
            return {"run_id": run.run_id, "status": run.status.value}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs")
    def list_runs(experiment_id: Optional[str] = None) -> list[dict]:
        runs = storage.list_runs(experiment_id)
        return [
            {
                "run_id": r.run_id,
                "experiment_id": r.experiment_id,
                "experiment_version": r.experiment_version,
                "status": r.status.value,
                "initiated_by": r.initiated_by,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "verdict": r.verdict,
                "manual_conclusion": r.manual_conclusion,
            }
            for r in runs
        ]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            r = storage.get_run(run_id)
            return {
                "run_id": r.run_id,
                "experiment_id": r.experiment_id,
                "experiment_version": r.experiment_version,
                "status": r.status.value,
                "initiated_by": r.initiated_by,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "verdict": r.verdict,
                "timings": r.timings,
                "summary": r.summary,
                "manual_conclusion": r.manual_conclusion,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: str) -> list[dict]:
        events = storage.list_events(run_id)
        return [
            {
                "run_id": e.run_id,
                "event_type": e.event_type,
                "ts": e.ts,
                "payload": e.payload,
            }
            for e in events
        ]

    @app.get("/api/runs/{run_id}/observation-window")
    def get_observation_window(run_id: str) -> dict:
        try:
            run = storage.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        sampling_interval = int(run.timings.get("sampling_interval_seconds", 0) or 0)
        if run.status.value == "completed":
            try:
                return _build_observation_window(
                    run_id=run.run_id,
                    experiment_id=run.experiment_id,
                    status=run.status.value,
                    timings=run.timings,
                    sampling_interval_seconds=sampling_interval,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        if run.status.value in {"stopped", "failed"} and run.ended_at:
            controlled = run.timings.get("controlled_observation_window")
            controlled_phases = (
                controlled.get("phases") if isinstance(controlled, dict) else None
            )
            if (
                isinstance(controlled_phases, dict)
                and {"baseline", "fault"}.issubset(controlled_phases)
            ):
                timings = run.timings
                window = controlled.get("window")
                phase_sources = controlled.get("phase_sources")
                post_marker = run.timings.get("post_observation_started")
                post_start = post_marker.get("at") if isinstance(post_marker, dict) else None
                post_end = window.get("end") if isinstance(window, dict) else None
                if (
                    "post" not in controlled_phases
                    and isinstance(post_start, str)
                    and isinstance(post_end, str)
                    and isinstance(phase_sources, dict)
                ):
                    timings = {
                        **run.timings,
                        "controlled_observation_window": {
                            **controlled,
                            "phases": {
                                **controlled_phases,
                                "post": {"start": post_start, "end": post_end},
                            },
                            "phase_sources": {
                                **phase_sources,
                                "post": "recovery_observation",
                            },
                        },
                    }
                try:
                    return _build_observation_window(
                        run_id=run.run_id,
                        experiment_id=run.experiment_id,
                        status=run.status.value,
                        timings=timings,
                        sampling_interval_seconds=sampling_interval,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            try:
                return _build_partial_observation_window(
                    run_id=run.run_id,
                    experiment_id=run.experiment_id,
                    status=run.status.value,
                    events=storage.list_events(run_id),
                    ended_at=run.ended_at,
                    sampling_interval_seconds=sampling_interval,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        raise HTTPException(
            status_code=409,
            detail="observation window is unavailable until the run has ended",
        )

    @app.get("/api/runs/{run_id}/mcp-evidence")
    def get_mcp_evidence(run_id: str, metric_id: Optional[str] = None) -> dict:
        window = get_observation_window(run_id)
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(
                run.experiment_id, run.experiment_version
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        metrics_config = (
            experiment.spec.get("observability", {}).get("metrics", {}) or {}
        )
        queries = metrics_config.get("queries", []) or []
        selected = next(
            (
                query
                for query in queries
                if metric_id
                and (query.get("id") or query.get("name")) == metric_id
            ),
            queries[0] if queries else None,
        )
        if not isinstance(selected, dict) or not selected.get("query"):
            raise HTTPException(
                status_code=409,
                detail="no PromQL query is configured for this experiment",
            )

        client = MCPPrometheusMetricsPlugin().client(metrics_config)
        result = investigate_metric_window(
            client,
            run_id=run_id,
            window_start=window["window"]["start"],
            window_end=window["window"]["end"],
            query=str(selected["query"]),
            step_seconds=int(
                window.get("sampling_interval_seconds")
                or metrics_config.get("step_seconds", 15)
            ),
        )
        selected_id = str(selected.get("id") or selected.get("name") or "query")
        artifact_path = storage.save_artifact(
            run_id, f"mcp_evidence_{selected_id}", result
        )
        result["artifact_path"] = artifact_path
        storage.append_event(
            DomainEvent(
                run_id=run_id,
                event_type="mcp_evidence_collected",
                ts=window["window"]["end"],
                payload={
                    "tool": result["tool"],
                    "metric_id": selected_id,
                    "window": result["window"],
                    "series_count": result["evidence"]["series_count"],
                    "sample_count": result["evidence"]["sample_count"],
                },
            )
        )
        return result

    @app.get("/api/runs/{run_id}/features")
    def get_run_features(run_id: str) -> dict:
        features = storage.get_run_features(run_id)
        if features is None:
            raise HTTPException(status_code=404, detail="features not found")
        return features

    @app.post("/api/runs/{run_id}/telemetry-chat")
    def telemetry_chat(run_id: str, req: TelemetryChatRequest) -> dict:
        window = get_observation_window(run_id)
        try:
            query_window, baseline_window = _select_telemetry_windows(
                window, req.window_scope
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(
                run.experiment_id, run.experiment_version
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        metrics_config = (
            experiment.spec.get("observability", {}).get("metrics", {}) or {}
        )
        mcp_url = metrics_config.get("mcp_sse_url")
        if not mcp_url:
            raise HTTPException(
                status_code=409,
                detail="experiment has no MCP metrics endpoint configured",
            )

        mcp_client = MCPToolClient(
            str(mcp_url), int(metrics_config.get("timeout_seconds", 10))
        )
        grafana_mcp_url = os.getenv("GRAFANA_MCP_SSE_URL", "").strip()
        if req.use_model and grafana_mcp_url:
            grafana_mcp_timeout = int(os.getenv("GRAFANA_MCP_TIMEOUT_SECONDS", "30"))
            mcp_client = CompositeMCPToolClient(
                [
                    mcp_client,
                    MCPToolClient(grafana_mcp_url, grafana_mcp_timeout),
                ]
            )

        tracer = get_tracer()
        with tracer.start_as_current_span("agent.telemetry_chat") as span:
            span.set_attribute(GenAI.OPERATION_NAME, "invoke_agent")
            span.set_attribute(GenAI.AGENT_NAME, "telemetry-chat")
            span.set_attribute(GenAI.PROVIDER_NAME, "custom")
            span.set_attribute(GenAI.SYSTEM, "qwen")
            if req.use_model:
                span.set_attribute(GenAI.REQUEST_MODEL, DEFAULT_LLM_MODEL)
            span.set_attribute("rca.run_id", run_id)
            span.set_attribute("rca.experiment_id", run.experiment_id)
            span.set_attribute("rca.observation_window.start", query_window["start"])
            span.set_attribute("rca.observation_window.end", query_window["end"])
            span.set_attribute("rca.observation_window.scope", req.window_scope)
            span.set_attribute("rca.run_window.start", window["window"]["start"])
            span.set_attribute("rca.run_window.end", window["window"]["end"])
            span.set_attribute("rca.agent.use_model", req.use_model)
            span.set_attribute("rca.agent.max_steps", req.max_steps)
            span.set_attribute(
                "rca.agent.grafana_mcp.enabled",
                bool(req.use_model and grafana_mcp_url),
            )
            try:
                chat_context = {
                    "start": query_window["start"],
                    "end": query_window["end"],
                    "namespace": str(
                        experiment.spec.get("scope", {}).get("namespace", "sock-shop")
                    ),
                }
                if req.use_model:
                    answer = answer_query_mcp(
                        req.query,
                        mcp_client=mcp_client,
                        **chat_context,
                        baseline_start=(
                            baseline_window.get("start") if baseline_window else None
                        ),
                        baseline_end=(
                            baseline_window.get("end") if baseline_window else None
                        ),
                        max_steps=req.max_steps,
                        max_new_tokens=req.max_new_tokens,
                    )
                else:
                    class _MCPPrometheusAdapter:
                        def query_range(
                            self, query: str, start: str, end: str, step: str
                        ):
                            return mcp_client.call_tool_json(
                                "prometheus_range_query",
                                {
                                    "query": query,
                                    "start": start,
                                    "end": end,
                                    "step": step,
                                },
                            )

                    answer = answer_query(
                        req.query,
                        prometheus=_MCPPrometheusAdapter(),
                        start=chat_context["start"],
                        end=chat_context["end"],
                        namespace=chat_context["namespace"],
                        use_model=False,
                        max_steps=req.max_steps,
                        max_new_tokens=req.max_new_tokens,
                    )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            span.set_attribute("rca.agent.answer_chars", len(answer))
            span.add_event(
                "agent.telemetry_chat.completed",
                {"query_chars": len(req.query), "answer_chars": len(answer)},
            )
        storage.append_event(
            DomainEvent(
                run_id=run_id,
                event_type="telemetry_chat_completed",
                ts=query_window["end"],
                payload={
                    "query_chars": len(req.query),
                    "answer_chars": len(answer),
                    "use_model": req.use_model,
                    "window": query_window,
                    "run_window": window["window"],
                    "baseline_window": baseline_window,
                    "window_scope": req.window_scope,
                },
            )
        )
        return {
            "run_id": run_id,
            "window": query_window,
            "run_window": window["window"],
            "baseline_window": baseline_window,
            "window_scope": req.window_scope,
            "phases": window.get("phases", {}),
            "phase_sources": window.get("phase_sources", {}),
            "configured_chaos_duration_seconds": (
                (experiment.spec.get("chaos_profile") or {}).get(
                    "fault_duration_seconds"
                )
            ),
            "fault_injection_duration_seconds": run.timings.get(
                "fault_injection_duration_seconds"
            ),
            "answer": answer,
            "used_mcp": True,
        }

    @app.post("/api/runs/{run_id}/rca")
    def run_rca(run_id: str) -> dict:
        window = get_observation_window(run_id)
        phases = window.get("phases") or {}
        if "baseline" not in phases or "fault" not in phases:
            raise HTTPException(
                status_code=409,
                detail="RCA requires baseline and fault phase windows",
            )
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(
                run.experiment_id, run.experiment_version
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        metrics_config = (
            experiment.spec.get("observability", {}).get("metrics", {}) or {}
        )
        traces_config = (
            experiment.spec.get("observability", {}).get("traces", {}) or {}
        )
        historical = experiment.spec.get("historical_observability") or {}
        historical_url = (
            historical.get("mcp_sse_url") or DEFAULT_HISTORICAL_MCP_SSE_URL
        )
        mcp_client = MCPToolClient(
            str(historical_url),
            _mcp_client_timeout_seconds(metrics_config, traces_config, historical),
        )
        namespace = str(
            experiment.spec.get("scope", {}).get("namespace", "sock-shop")
        )
        tracer = get_tracer()
        with tracer.start_as_current_span("agent.run_rca") as span:
            span.set_attribute(GenAI.OPERATION_NAME, "invoke_agent")
            span.set_attribute(GenAI.AGENT_NAME, "run-rca")
            span.set_attribute(GenAI.PROVIDER_NAME, "custom")
            span.set_attribute(GenAI.SYSTEM, "qwen")
            span.set_attribute(GenAI.REQUEST_MODEL, DEFAULT_LLM_MODEL)
            span.set_attribute("rca.run_id", run_id)
            span.set_attribute("rca.experiment_id", run.experiment_id)
            span.set_attribute("rca.agent.max_tool_calls", 10)
            rca_phases, reference_source = rca_reference_phases(
                phases, baseline_source=(window.get("phase_sources") or {}).get("baseline"),
            )
            span.set_attribute("rca.reference_source", reference_source)
            try:
                result = answer_run_rca_mcp(
                    mcp_client=mcp_client,
                    phases=rca_phases,
                    namespace=namespace,
                    max_iterations=10,
                )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            trace = {
                "run_id": run_id,
                "window": window,
                "rca_phases": rca_phases,
                "reference_source": reference_source,
                "namespace": namespace,
                **result,
            }
            artifact_path = storage.save_artifact(run_id, "rca_investigation", trace)
            tool_names = [call["tool"] for call in result["calls"]]
            span.set_attribute("rca.agent.iterations", result["iterations"])
            span.set_attribute("rca.agent.tool_calls", len(result["calls"]))
            span.set_attribute("rca.agent.stop_reason", result["stop_reason"])
            span.set_attribute("rca.agent.answer_chars", len(result["answer"]))

        storage.append_event(
            DomainEvent(
                run_id=run_id,
                event_type="rca_investigation_completed",
                ts=window["window"]["end"],
                payload={
                    "tools": tool_names,
                    "iterations": result["iterations"],
                    "stop_reason": result["stop_reason"],
                    "window": window["window"],
                    "artifact_path": artifact_path,
                },
            )
        )
        return {
            "run_id": run_id,
            "window": window["window"],
            "answer": result["answer"],
            "tools_used": tool_names,
            "iterations": result["iterations"],
            "stop_reason": result["stop_reason"],
            "artifact_path": artifact_path,
            "evidence_bundle": result.get("evidence_bundle", {}),
            "used_mcp": True,
            "observability_source": "history",
        }

    @app.get("/api/runs/{run_id}/multiagent-rca/plan", response_model=InvestigationPlan)
    def get_multiagent_rca_plan(run_id: str) -> InvestigationPlan:
        window = get_observation_window(run_id)
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(run.experiment_id, run.experiment_version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        namespace = experiment.spec.get("scope", {}).get("namespace", "sock-shop")
        try:
            context = _build_multiagent_context(run_id, namespace, window)
        except (ValueError, ValidationError, SystemCardError, OSError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Multiagent planning requires valid windows, namespace and system card: {exc}",
            ) from exc
        return build_investigation_plan(context)

    @app.post("/api/runs/{run_id}/multiagent-rca")
    def run_multiagent_rca(
        run_id: str, options: RCABenchmarkOptions | None = None
    ) -> dict:
        options = options or RCABenchmarkOptions()
        window = get_observation_window(run_id)
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(run.experiment_id, run.experiment_version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        namespace = experiment.spec.get("scope", {}).get("namespace", "sock-shop")
        try:
            context = _build_multiagent_context(
                run_id,
                namespace,
                window,
                possible_fault_types=options.possible_fault_types,
            )
        except (ValueError, ValidationError, SystemCardError, OSError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Multiagent RCA requires valid windows, namespace and system card: {exc}",
            ) from exc

        observability = experiment.spec.get("observability") or {}
        metrics_config = observability.get("metrics") or {}
        traces_config = observability.get("traces") or {}
        historical = experiment.spec.get("historical_observability") or {}
        historical_url = (
            historical.get("mcp_sse_url") or DEFAULT_HISTORICAL_MCP_SSE_URL
        )
        timeout = _mcp_client_timeout_seconds(
            metrics_config, traces_config, historical
        )
        attempt_id = uuid4().hex
        with get_tracer().start_as_current_span("agent.multiagent_rca") as span:
            span.set_attribute("rca.run_id", run_id)
            span.set_attribute("rca.attempt_id", attempt_id)
            span.set_attribute(GenAI.REQUEST_MODEL, DEFAULT_LLM_MODEL)
            result = answer_multiagent_rca(
                mcp_client=MCPToolClient(str(historical_url), timeout),
                context=context,
                budget=InvestigationBudget(),
                model=DEFAULT_LLM_MODEL,
            )
            result.update(
                {
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "window": window["window"],
                    "model": DEFAULT_LLM_MODEL,
                    "observability_source": "history",
                }
            )
            artifact_path = storage.save_artifact(
                run_id, f"multiagent_rca_{attempt_id}", result
            )
            result["artifact_path"] = artifact_path
            if result["status"] == "error":
                span.set_status(Status(StatusCode.ERROR, result.get("error") or "Multiagent RCA failed"))
        storage.append_event(
            DomainEvent(
                run_id=run_id,
                event_type=(
                    "multiagent_rca_completed"
                    if result["status"] != "error"
                    else "multiagent_rca_failed"
                ),
                ts=result["generated_at"],
                payload={
                    "attempt_id": attempt_id,
                    "status": result["status"],
                    "artifact_path": artifact_path,
                },
            )
        )
        if result["status"] == "error":
            raise HTTPException(
                status_code=502,
                detail={
                    "error": result.get("error") or "Multiagent RCA failed",
                    "artifact_path": artifact_path,
                    "attempt_id": attempt_id,
                },
            )
        return result

    @app.post("/api/runs/{run_id}/skill-rca")
    def run_skill_rca(
        run_id: str,
        options: RCABenchmarkOptions | None = None,
        history_campaign_id: str | None = None,
    ) -> dict:
        options = options or RCABenchmarkOptions()
        window = get_observation_window(run_id)
        phases = window.get("phases") or {}
        if not {"baseline", "fault"}.issubset(phases):
            raise HTTPException(
                status_code=409, detail="Skill RCA requires baseline and fault windows"
            )
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(
                run.experiment_id, run.experiment_version
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        observability = experiment.spec.get("observability") or {}
        metrics_config = observability.get("metrics") or {}
        traces_config = observability.get("traces") or {}
        historical = experiment.spec.get("historical_observability") or {}
        historical_url = (
            historical.get("mcp_sse_url") or DEFAULT_HISTORICAL_MCP_SSE_URL
        )
        timeout = _mcp_client_timeout_seconds(
            metrics_config, traces_config, historical
        )
        archive_campaign_id = history_campaign_id or run.experiment_id
        archive_traces = None
        archive_status = "not_configured"
        if history_archive is not None:
            try:
                archive_traces = history_archive.read_trace_evidence(
                    campaign_id=archive_campaign_id,
                    run_id=run_id,
                    phases=phases,
                )
                archive_status = str(archive_traces.get("status", "unknown"))
            except HistoryArchiveError as exc:
                archive_status = "unavailable"
                archive_traces = {
                    "status": "tool_error",
                    "evidence_source": "archive",
                    "campaign_id": archive_campaign_id,
                    "run_id": run_id,
                    "error": str(exc),
                }
        attempt_id = uuid4().hex
        with get_tracer().start_as_current_span("agent.skill_rca") as span:
            span.set_attribute("rca.run_id", run_id)
            span.set_attribute("rca.attempt_id", attempt_id)
            span.set_attribute(GenAI.REQUEST_MODEL, DEFAULT_LLM_MODEL)
            try:
                result = answer_skill_rca(
                    mcp_client=MCPToolClient(str(historical_url), timeout),
                    phases=phases,
                    namespace=str(experiment.spec.get("scope", {}).get("namespace", "sock-shop")),
                    window_metadata={
                        key: window[key]
                        for key in ("phase_sources", "partial", "sampling_interval_seconds")
                        if key in window
                    },
                    possible_fault_types=options.possible_fault_types,
                    require_best_guess=options.require_best_guess,
                    archive_traces=archive_traces,
                    evidence_source="history",
                )
            except (SkillRCAError, OSError) as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            result.update(
                {
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "window": window["window"],
                    "model": DEFAULT_LLM_MODEL,
                    "history_archive": {
                        "status": archive_status,
                        "campaign_id": (
                            archive_campaign_id if history_archive is not None else None
                        ),
                        "run_id": run_id,
                        "archive_key": (
                            archive_traces.get("archive_key")
                            if isinstance(archive_traces, dict)
                            else None
                        ),
                    },
                    "observability_source": "history",
                }
            )
            artifact_path = storage.save_artifact(
                run_id, f"skill_rca_{attempt_id}", result
            )
            result["artifact_path"] = artifact_path
            if result["status"] == "error":
                span.set_status(Status(StatusCode.ERROR, result["error"]))
        storage.append_event(
            DomainEvent(
                run_id=run_id,
                event_type="skill_rca_completed" if result["status"] != "error" else "skill_rca_failed",
                ts=result["generated_at"],
                payload={
                    "attempt_id": attempt_id,
                    "status": result["status"],
                    "artifact_path": artifact_path,
                },
            )
        )
        if result["status"] == "error":
            raise HTTPException(
                status_code=502,
                detail={"error": result["error"], "artifact_path": artifact_path, "attempt_id": attempt_id},
            )
        return result

    @app.get("/api/runs/{run_id}/summary")
    def get_run_summary(run_id: str) -> dict:
        summary = storage.get_run_summary(run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="summary not found")
        return summary

    @app.get("/api/runs/{run_id}/history/validation")
    def validate_run_history(run_id: str) -> dict:
        try:
            run = storage.get_run(run_id)
            experiment = storage.get_experiment(
                run.experiment_id, run.experiment_version
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        historical = experiment.spec.get("historical_observability") or {}
        migration = run.summary.get("history_migration") or {}
        if historical.get("mode") == "dual_write":
            mcp_url = historical.get("mcp_sse_url")
            service = (experiment.spec.get("controlled_campaign") or {}).get(
                "service"
            )
            namespace = (experiment.spec.get("scope") or {}).get(
                "namespace", "sock-shop"
            )
            if not isinstance(mcp_url, str) or not mcp_url:
                return {
                    "status": "failed",
                    "campaign_id": run.experiment_id,
                    "run_id": run_id,
                    "migration": migration,
                    "error": "dual-write validation requires the historical MCP URL",
                }
            if not isinstance(service, str) or not service:
                return {
                    "status": "failed",
                    "campaign_id": run.experiment_id,
                    "run_id": run_id,
                    "migration": migration,
                    "error": "dual-write validation requires a controlled target service",
                }
            if not isinstance(namespace, str) or not namespace:
                raise HTTPException(
                    status_code=409,
                    detail="dual-write validation requires a namespace",
                )
            timeout = historical.get("timeout_seconds", 30)
            if type(timeout) is not int or timeout < 1:
                raise HTTPException(
                    status_code=409,
                    detail="historical timeout_seconds must be a positive integer",
                )
            try:
                window = get_observation_window(run_id)
                result = validate_history_stream(
                    MCPToolClient(mcp_url, timeout),
                    phases=window.get("phases") or {},
                    namespace=namespace,
                    service=service,
                )
            except ValueError as exc:
                return {
                    "status": "failed",
                    "mode": "dual_write",
                    "campaign_id": run.experiment_id,
                    "run_id": run_id,
                    "migration": migration,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            except (OSError, RuntimeError, TimeoutError) as exc:
                return {
                    "status": "pending",
                    "mode": "dual_write",
                    "campaign_id": run.experiment_id,
                    "run_id": run_id,
                    "migration": migration,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            return {
                **result,
                "campaign_id": run.experiment_id,
                "run_id": run_id,
                "migration": migration,
            }
        if history_archive is None:
            raise HTTPException(
                status_code=503, detail="Historical archive reader is not configured"
            )
        if migration.get("status") in {
            "dispatch_failed",
            "blocked_missing_window",
            "disabled",
        }:
            return {
                "status": "failed",
                "run_id": run_id,
                "migration": migration,
            }
        try:
            manifest = history_archive.read_manifest(
                campaign_id=run.experiment_id,
                run_id=run_id,
            )
        except HistoryArchiveNotFound:
            status = "pending" if migration.get("status") == "queued" else "failed"
            return {
                "status": status,
                "run_id": run_id,
                "migration": migration,
            }
        except HistoryArchiveError as exc:
            raise HTTPException(
                status_code=503, detail=f"Could not validate history archive: {exc}"
            ) from exc

        telemetry = manifest.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        source_counts = {
            "metrics": (telemetry.get("metrics") or {}).get("source_samples"),
            "logs": (telemetry.get("logs") or {}).get("source_entries"),
            "traces": (telemetry.get("traces") or {}).get("source_traces"),
            "spans": (telemetry.get("traces") or {}).get("source_spans"),
        }
        families_complete = all(
            isinstance(telemetry.get(family), dict)
            and telemetry[family].get("status") == "complete"
            for family in ("metrics", "logs", "traces")
        )
        counts_present = all(
            isinstance(count, int) and not isinstance(count, bool) and count > 0
            for count in source_counts.values()
        )
        complete = (
            manifest.get("overall_status") == "complete"
            and families_complete
            and counts_present
        )
        return {
            "status": "complete" if complete else "partial",
            "campaign_id": run.experiment_id,
            "run_id": run_id,
            "manifest_overall_status": manifest.get("overall_status"),
            "telemetry": {
                family: (telemetry.get(family) or {}).get("status")
                for family in ("metrics", "logs", "traces")
            },
            "source_counts": source_counts,
        }

    @app.get("/api/runs/{run_id}/similar")
    def get_similar_runs(
        run_id: str,
        limit: int = 5,
        chaos_type: Optional[str] = None,
        verdict: Optional[str] = None,
        mode: str = "embedding",
    ) -> dict:
        target = storage.get_run_summary(run_id)
        if target is None:
            raise HTTPException(status_code=404, detail="summary not found")

        target_experiment_id = target.get("experiment_id")

        # Phase F1 — hybrid mode: cosine(embedding) + propagation-graph similarity.
        if mode == "hybrid":
            target_emb = storage.get_run_embedding(run_id)
            target_features = storage.get_run_features(run_id) or {}
            target_graph = target_features.get("propagation_graph") or {}
            provider = get_embedding_provider()
            if target_emb is None or target_emb["provider"] != provider.name:
                # Without an embedding we cannot do hybrid; fall back to tags.
                mode = "tags"
            else:
                target_vec = blob_to_vector(target_emb["blob"], target_emb["dim"])
                rows = storage.iter_run_embeddings(
                    provider=target_emb["provider"],
                    chaos_type=chaos_type,
                    experiment_id=target_experiment_id,
                    limit=1000,
                    include_features=True,
                )
                candidates = []
                for row in rows:
                    if verdict and row["verdict"] != verdict:
                        continue
                    try:
                        row["vector"] = blob_to_vector(row["blob"], row["dim"])
                    except ValueError:
                        continue
                    row["graph"] = (row.get("features") or {}).get(
                        "propagation_graph"
                    ) or {}
                    candidates.append(row)
                neighbours = rank_hybrid(
                    target_vec,
                    target_graph,
                    candidates,
                    limit=limit,
                    exclude_run_id=run_id,
                )
                return {
                    "run_id": run_id,
                    "mode": "hybrid",
                    "query_tags": target.get("tags") or [],
                    "query_cascade": [
                        c.get("service")
                        for c in (target_graph.get("cascade_order") or [])
                        if isinstance(c, dict)
                    ],
                    "neighbours": [
                        {
                            "run_id": n["run_id"],
                            "experiment_id": n["experiment_id"],
                            "chaos_type": n["chaos_type"],
                            "verdict": n["verdict"],
                            "score": n["score"],
                            "semantic_score": n.get("semantic_score"),
                            "graph_score": n.get("graph_score"),
                            "cascade_overlap_services": n.get(
                                "cascade_overlap_services", []
                            ),
                            "graph_detail": n.get("graph_detail", {}),
                            "tags": n.get("tags") or [],
                            "created_at": n["created_at"],
                        }
                        for n in neighbours
                    ],
                }

        use_embedding = mode == "embedding"
        neighbours: list[dict] = []
        used_mode = mode

        if use_embedding:
            target_emb = storage.get_run_embedding(run_id)
            if target_emb is None:
                # Fall back to tag mode if target has no embedding yet
                use_embedding = False
                used_mode = "tags"
            else:
                provider = get_embedding_provider()
                if target_emb["provider"] != provider.name:
                    used_mode = "tags"
                    use_embedding = False

        if use_embedding:
            target_vec = blob_to_vector(target_emb["blob"], target_emb["dim"])
            rows = storage.iter_run_embeddings(
                provider=target_emb["provider"],
                chaos_type=chaos_type,
                experiment_id=target_experiment_id,
                limit=1000,
            )
            candidates = []
            for row in rows:
                if verdict and row["verdict"] != verdict:
                    continue
                try:
                    row["vector"] = blob_to_vector(row["blob"], row["dim"])
                except ValueError:
                    continue
                candidates.append(row)
            neighbours = rank_by_embedding(
                target_vec, candidates, limit=limit, exclude_run_id=run_id
            )
        else:
            candidates = storage.list_run_summaries(
                experiment_id=target_experiment_id,
                chaos_type=chaos_type or target.get("chaos_type"),
                verdict=verdict,
                limit=500,
            )
            neighbours = rank_similar_runs(
                target.get("tags") or [],
                candidates,
                limit=limit,
                exclude_run_id=run_id,
            )

        return {
            "run_id": run_id,
            "mode": used_mode,
            "query_tags": target.get("tags") or [],
            "neighbours": [
                {
                    "run_id": n["run_id"],
                    "experiment_id": n["experiment_id"],
                    "chaos_type": n["chaos_type"],
                    "verdict": n["verdict"],
                    "score": n["score"],
                    "tags": n.get("tags") or [],
                    "created_at": n["created_at"],
                }
                for n in neighbours
            ],
        }

    @app.get("/api/runs/{run_id}/rag-context")
    def get_rag_context(run_id: str, limit: int = 3, mode: str = "embedding") -> dict:
        """Bundle target summary + top-K neighbour summaries for prompt assembly."""
        return _build_rag_context(run_id, limit, mode)

    def _build_rag_context(run_id: str, limit: int, mode: str) -> dict:
        target = storage.get_run_summary(run_id)
        if target is None:
            raise HTTPException(status_code=404, detail="summary not found")

        run = storage.get_run(run_id)
        artifact_paths = run.summary.get("artifact_paths", {})
        observability_path = artifact_paths.get("observability_summary")
        if observability_path:
            try:
                target["observability_summary"] = json.loads(
                    Path(observability_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                target["observability_summary"] = None

        target_experiment_id = target.get("experiment_id")

        # Phase F1 — hybrid retrieval blends embedding cosine + propagation graph.
        if mode == "hybrid":
            target_emb = storage.get_run_embedding(run_id)
            target_features = storage.get_run_features(run_id) or {}
            target_graph = target_features.get("propagation_graph") or {}
            provider = get_embedding_provider()
            if target_emb is not None and target_emb["provider"] == provider.name:
                target_vec = blob_to_vector(target_emb["blob"], target_emb["dim"])
                rows = storage.iter_run_embeddings(
                    provider=target_emb["provider"],
                    experiment_id=target_experiment_id,
                    limit=1000,
                    include_features=True,
                )
                candidates = []
                for row in rows:
                    try:
                        row["vector"] = blob_to_vector(row["blob"], row["dim"])
                    except ValueError:
                        continue
                    row["graph"] = (row.get("features") or {}).get(
                        "propagation_graph"
                    ) or {}
                    candidates.append(row)
                ranked = rank_hybrid(
                    target_vec,
                    target_graph,
                    candidates,
                    limit=limit,
                    exclude_run_id=run_id,
                )
                return {
                    "mode": "hybrid",
                    "target": target,
                    "neighbours": [
                        {
                            "run_id": n["run_id"],
                            "score": n["score"],
                            "semantic_score": n.get("semantic_score"),
                            "graph_score": n.get("graph_score"),
                            "cascade_overlap_services": n.get(
                                "cascade_overlap_services", []
                            ),
                            "verdict": n["verdict"],
                            "summary_text": n.get("summary_text", ""),
                        }
                        for n in ranked
                    ],
                }
            # No embedding → fall through to tag mode.
            mode = "tags"

        use_embedding = mode == "embedding"
        target_emb = storage.get_run_embedding(run_id) if use_embedding else None
        provider = get_embedding_provider() if use_embedding else None
        if use_embedding and (
            target_emb is None
            or (provider is not None and target_emb["provider"] != provider.name)
        ):
            use_embedding = False

        if use_embedding and target_emb is not None:
            target_vec = blob_to_vector(target_emb["blob"], target_emb["dim"])
            rows = storage.iter_run_embeddings(
                provider=target_emb["provider"],
                experiment_id=target_experiment_id,
                limit=1000,
            )
            candidates = []
            for row in rows:
                try:
                    row["vector"] = blob_to_vector(row["blob"], row["dim"])
                except ValueError:
                    continue
                candidates.append(row)
            ranked = rank_by_embedding(
                target_vec, candidates, limit=limit, exclude_run_id=run_id
            )
            used_mode = "embedding"
        else:
            cand = storage.list_run_summaries(
                experiment_id=target_experiment_id,
                limit=500,
            )
            ranked = rank_similar_runs(
                target.get("tags") or [],
                cand,
                limit=limit,
                exclude_run_id=run_id,
            )
            used_mode = "tags"

        return {
            "mode": used_mode,
            "target": target,
            "neighbours": [
                {
                    "run_id": n["run_id"],
                    "score": n["score"],
                    "verdict": n["verdict"],
                    "summary_text": n.get("summary_text", ""),
                }
                for n in ranked
            ],
        }

    @app.get("/api/embeddings/info")
    def embeddings_info() -> dict:
        provider = get_embedding_provider()
        pending = storage.list_runs_without_embedding(
            provider=provider.name, limit=1000
        )
        return {
            "provider": provider.name,
            "dim": provider.dim,
            "pending_count": len(pending),
        }

    @app.post("/api/features/reindex")
    def reindex_embeddings(limit: int = 500) -> dict:
        """Recompute embeddings for runs missing them under the current provider."""
        provider = get_embedding_provider()
        pending = storage.list_runs_without_embedding(
            provider=provider.name, limit=limit
        )
        if not pending:
            return {
                "provider": provider.name,
                "dim": provider.dim,
                "indexed": 0,
                "skipped": 0,
            }
        texts = [p["summary_text"] for p in pending]
        vectors = provider.embed_batch(texts)
        indexed = 0
        skipped = 0
        for row, vec in zip(pending, np.asarray(vectors)):
            try:
                storage.upsert_run_embedding(
                    row["run_id"], provider.name, provider.dim, vector_to_blob(vec)
                )
                indexed += 1
            except Exception:  # pragma: no cover - storage best-effort
                skipped += 1
        return {
            "provider": provider.name,
            "dim": provider.dim,
            "indexed": indexed,
            "skipped": skipped,
        }

    # ── LLM verdict (Phase D) ────────────────────────────────────────
    @app.get("/api/llm/health")
    def get_llm_health() -> dict:
        return llm_health()

    # ── System cards (persistent topology context) ───────────────────
    @app.get("/api/system-cards")
    def list_cards() -> dict:
        return {"system_ids": list_system_cards()}

    @app.get("/api/system-cards/{system_id}")
    def get_system_card(system_id: str) -> dict:
        meta = system_card_meta(system_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="system card not found")
        return {**meta, "rendered": render_system_card(system_id)}

    @app.post("/api/runs/{run_id}/llm-analysis")
    def run_llm_analysis(
        run_id: str,
        limit: int = 3,
        mode: str = "embedding",
        max_new_tokens: int = 512,
        budget_tokens: int = 3200,
        force: bool = False,
        system_id: str = "sock-shop",
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        eval_target: Optional[str] = None,
        eval_fault: Optional[str] = None,
        eval_strategy: Optional[str] = None,
    ) -> dict:
        """Assemble RAG prompt, call LLM, persist parsed verdict."""

        def _trim_for_span(text: Optional[str], limit: int = 8000) -> str:
            if not text:
                return ""
            if len(text) <= limit:
                return text
            return text[:limit] + "...[truncated]"

        def _log(message: str) -> None:
            strategy = eval_strategy or "n/a"
            print(
                f"[llm-analysis] run={run_id} strategy={strategy} {message}",
                flush=True,
            )

        def _record_usage(span, usage: dict) -> None:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            if usage.get("source") != "measured":
                return
            span.set_attribute(GenAI.USAGE_INPUT_TOKENS, input_tokens)
            span.set_attribute(GenAI.USAGE_OUTPUT_TOKENS, output_tokens)
            span.set_attribute("gen_ai.usage.total_tokens", input_tokens + output_tokens)
            span.set_attribute("rca.llm.token_count_source", "measured")
            record_llm_metrics(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=target_model,
            )

        # Resolve LLM target URL and model name for this request.
        # Fall back to environment defaults (Qwen local) if not explicitly passed.
        prov = (provider or "").strip().lower()
        target_timeout = DEFAULT_LLM_TIMEOUT
        if prov in {
            "gemini",
            "gemini-flash",
            "gemini-2.0-flash",
            "gemini-2.5-flash",
            "google",
        }:
            target_model = model or "gemini-2.5-flash"
            target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"
            target_api_style = "gemini"
            target_timeout = float(os.environ.get("GEMINI_LLM_TIMEOUT_SECONDS", "60"))
        elif prov in {"gpt", "gpt-4o-mini", "openai", "github"}:
            env_url = os.environ.get("LLM_URL")
            if env_url and "localhost" not in env_url and "127.0.0.1" not in env_url:
                target_url = env_url
            else:
                target_url = "https://api.openai.com/v1/chat/completions"
            target_model = model or os.environ.get("LLM_MODEL") or "gpt-4o-mini"
            target_api_style = "openai"
        elif prov in {"qwen", "local"}:
            target_url = "http://localhost:8001/v1/chat/completions"
            target_model = model or "qwen-14b"
            target_api_style = "openai"
        elif prov:
            target_url = prov  # custom URL passed directly
            target_model = model or DEFAULT_LLM_MODEL
            target_api_style = None
        else:
            target_url = DEFAULT_LLM_URL
            target_model = model or DEFAULT_LLM_MODEL
            target_api_style = None

        _log(
            "start "
            f"provider={provider or 'default'} style={target_api_style or 'auto'} "
            f"model={target_model} mode={mode} limit={limit} "
            f"budget_tokens={budget_tokens} max_new_tokens={max_new_tokens} "
            f"llm_timeout={target_timeout}s force={force}"
        )
        request_started = time.monotonic()
        timing_meta = {
            "rag_ms": None,
            "prompt_ms": None,
            "llm_ms": None,
            "parse_ms": None,
            "total_ms": None,
        }

        tracer = get_tracer()
        llm_sys = "qwen" if "qwen" in target_model.lower() else "llm"
        audit_base = {
            "event": "llm.request_response",
            "run_id": run_id,
            "eval_target": eval_target,
            "eval_fault": eval_fault,
            "eval_strategy": eval_strategy,
            "rag_mode_requested": mode,
            "rag_limit": int(limit),
            "system_card_id": system_id,
            "budget_tokens": int(budget_tokens),
            "max_new_tokens": int(max_new_tokens),
            "model": target_model,
        }
        with tracer.start_as_current_span("rca.analyze") as root:
            root.set_attribute(GenAI.OPERATION_NAME, "invoke_agent")
            root.set_attribute(GenAI.SYSTEM, llm_sys)
            root.set_attribute("rca.run_id", run_id)
            if eval_target:
                root.set_attribute("rca.eval.target", eval_target)
            if eval_fault:
                root.set_attribute("rca.eval.fault", eval_fault)
            if eval_strategy:
                root.set_attribute("rca.eval.strategy", eval_strategy)
            root.set_attribute(GenAI.RAG_MODE, mode)
            root.set_attribute("rca.rag.limit", limit)
            root.set_attribute("rca.system_card_id", system_id)
            root.set_attribute(GenAI.REQUEST_MAX_TOKENS, max_new_tokens)
            root.set_attribute("rca.budget_tokens", budget_tokens)

            if not force:
                cached = storage.get_llm_analysis(run_id)
                if cached is not None:
                    root.set_attribute("rca.cached", True)
                    return {**cached, "cached": True}
            root.set_attribute("rca.cached", False)

            # ── RAG retrieval ────────────────────────────────────────────
            with tracer.start_as_current_span("rag.retrieve") as span:
                stage_started = time.monotonic()
                rag = _build_rag_context(run_id, limit, mode)
                neighbours = rag.get("neighbours") or []
                timing_meta["rag_ms"] = int((time.monotonic() - stage_started) * 1000)
                _log(
                    f"rag.done elapsed={timing_meta['rag_ms'] / 1000:.2f}s "
                    f"effective_mode={rag.get('mode') or mode} neighbours={len(neighbours)}"
                )
                span.set_attribute(GenAI.RAG_MODE, rag.get("mode") or mode)
                span.set_attribute("rca.rag.neighbours_returned", len(neighbours))
                span.set_attribute("rca.rag.empty", not neighbours)
                scores = [
                    n.get("score")
                    for n in neighbours
                    if isinstance(n.get("score"), (int, float))
                ]
                if scores:
                    span.set_attribute("rca.rag.top_score", float(max(scores)))

            # ── Prompt assembly ──────────────────────────────────────────
            with tracer.start_as_current_span("prompt.assemble") as span:
                stage_started = time.monotonic()
                prompt, meta = assemble_prompt(
                    rag,
                    budget_tokens=budget_tokens,
                    max_neighbours=limit,
                    system_id=system_id,
                )
                span.set_attribute(
                    "rca.prompt.tokens_estimate",
                    int(meta.get("prompt_tokens_estimate") or 0),
                )
                span.set_attribute(
                    "rca.prompt.neighbour_count", int(meta.get("neighbour_count") or 0)
                )
                span.set_attribute(
                    "rca.system_card_id", str(meta.get("system_card_id") or system_id)
                )
                prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
                meta["prompt_sha256"] = prompt_sha
                timing_meta["prompt_ms"] = int(
                    (time.monotonic() - stage_started) * 1000
                )
                _log(
                    f"prompt.done elapsed={timing_meta['prompt_ms'] / 1000:.2f}s "
                    f"tokens_est={int(meta.get('prompt_tokens_estimate') or 0)} "
                    f"neighbours={int(meta.get('neighbour_count') or 0)} "
                    f"sha256={prompt_sha}"
                )
                if os.environ.get("RCA_PRINT_PROMPT", "false").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    _log(f"prompt.content\n{prompt}")
                if capture_content():
                    span.set_attribute(GenAI.PROMPT, prompt)

            # ── LLM call ─────────────────────────────────────────────────
            t_call_start = time.time()
            usage = {}
            with tracer.start_as_current_span("gen_ai.chat") as span:
                span.set_attribute(GenAI.OPERATION_NAME, "chat")
                span.set_attribute(GenAI.SYSTEM, llm_sys)
                span.set_attribute(GenAI.REQUEST_MODEL, target_model)
                span.set_attribute(GenAI.REQUEST_MAX_TOKENS, max_new_tokens)
                span.set_attribute("rca.llm.attempt", 1)
                if eval_target:
                    span.set_attribute("rca.eval.target", eval_target)
                if eval_fault:
                    span.set_attribute("rca.eval.fault", eval_fault)
                if eval_strategy:
                    span.set_attribute("rca.eval.strategy", eval_strategy)
                request_event = {
                    "attempt": 1,
                    "retry": False,
                    "prompt_chars": len(prompt),
                    "prompt_sha256": prompt_sha,
                }
                if capture_content():
                    request_event["prompt"] = _trim_for_span(prompt)
                span.add_event("llm.request", request_event)
                _log("llm.call.start attempt=1")
                try:
                    raw = call_llm(
                        prompt,
                        max_new_tokens=max_new_tokens,
                        url=target_url,
                        model=target_model,
                        api_style=target_api_style,
                        api_key=api_key,
                        timeout=target_timeout,
                        usage=usage,
                    )
                except LLMClientError as exc:
                    record_llm_io(
                        {
                            **audit_base,
                            "attempt": 1,
                            "retry": False,
                            "status": "error",
                            "error": str(exc),
                            "prompt": prompt,
                        }
                    )
                    span.add_event(
                        "llm.response_error",
                        {
                            "attempt": 1,
                            "retry": False,
                            "error": _trim_for_span(str(exc), 2000),
                        },
                    )
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    _log(
                        f"llm.call.error attempt=1 error={_trim_for_span(str(exc), 500)}"
                    )
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                if capture_content():
                    span.set_attribute(GenAI.COMPLETION, raw)
                _record_usage(span, usage)
                response_event = {
                    "attempt": 1,
                    "retry": False,
                    "response_chars": len(raw),
                    "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12],
                }
                if capture_content():
                    response_event["response"] = _trim_for_span(raw)
                span.add_event("llm.response", response_event)

            llm_latency_ms = int((time.time() - t_call_start) * 1000)
            timing_meta["llm_ms"] = llm_latency_ms
            _log(
                f"llm.call.done attempt=1 elapsed_ms={llm_latency_ms} raw_chars={len(raw)}"
            )
            with tracer.start_as_current_span("response.parse") as span:
                stage_started = time.monotonic()
                parsed = parse_verdict_response(raw)
                timing_meta["parse_ms"] = int((time.monotonic() - stage_started) * 1000)
                _log(
                    "parse.done attempt=1 "
                    f"parse_error={parsed.get('parse_error') or ''} "
                    f"rca={parsed.get('rca') or ''} "
                    f"fault_category={parsed.get('fault_category') or ''}"
                )
                span.set_attribute("rca.parse_error", parsed.get("parse_error") or "")
                span.set_attribute("rca.rca_present", bool(parsed.get("rca")))
                span.add_event(
                    "llm.parse",
                    {
                        "attempt": 1,
                        "retry": False,
                        "parse_error": str(parsed.get("parse_error") or ""),
                        "rca_present": bool(parsed.get("rca")),
                        "fault_category_present": bool(parsed.get("fault_category")),
                    },
                )
            record_llm_io(
                {
                    **audit_base,
                    "attempt": 1,
                    "retry": False,
                    "status": "ok",
                    "prompt": prompt,
                    "response": raw,
                    "parse_error": parsed.get("parse_error"),
                    "rca_present": bool(parsed.get("rca")),
                    "fault_category_present": bool(parsed.get("fault_category")),
                    "prompt_tokens_estimate": int(
                        (meta or {}).get("prompt_tokens_estimate") or 0
                    ),
                    "neighbour_count": int((meta or {}).get("neighbour_count") or 0),
                }
            )

            # Single retry when the first attempt failed to produce a usable
            # verdict (empty response, JSON parse error, or no `rca` field).
            # The retry is cheap, scoped, and avoids leaving the eval row
            # blank — which would also blind both post-processors below.
            retry_count = 0
            if not parsed.get("rca") or parsed.get("parse_error"):
                retry_count = 1
                _log("retry.start reason=parse_error_or_missing_rca")
                with tracer.start_as_current_span("gen_ai.chat") as span:
                    span.set_attribute(GenAI.OPERATION_NAME, "chat")
                    span.set_attribute(GenAI.SYSTEM, llm_sys)
                    span.set_attribute("rca.retry", True)
                    span.set_attribute("rca.llm.attempt", 2)
                    if eval_target:
                        span.set_attribute("rca.eval.target", eval_target)
                    if eval_fault:
                        span.set_attribute("rca.eval.fault", eval_fault)
                    if eval_strategy:
                        span.set_attribute("rca.eval.strategy", eval_strategy)
                    request_event = {
                        "attempt": 2,
                        "retry": True,
                        "prompt_chars": len(prompt),
                        "prompt_sha256": prompt_sha,
                    }
                    if capture_content():
                        request_event["prompt"] = _trim_for_span(prompt)
                    span.add_event("llm.request", request_event)
                    try:
                        usage_retry = {}
                        raw_retry = call_llm(
                            prompt,
                            max_new_tokens=max_new_tokens,
                            url=target_url,
                            model=target_model,
                            api_style=target_api_style,
                            api_key=api_key,
                            timeout=target_timeout,
                            usage=usage_retry,
                        )
                        _record_usage(span, usage_retry)
                        parsed_retry = parse_verdict_response(raw_retry)
                        _log(
                            "parse.done attempt=2 "
                            f"parse_error={parsed_retry.get('parse_error') or ''} "
                            f"rca={parsed_retry.get('rca') or ''} "
                            f"fault_category={parsed_retry.get('fault_category') or ''}"
                        )
                        response_event = {
                            "attempt": 2,
                            "retry": True,
                            "response_chars": len(raw_retry),
                            "response_sha256": hashlib.sha256(
                                raw_retry.encode("utf-8")
                            ).hexdigest()[:12],
                        }
                        if capture_content():
                            response_event["response"] = _trim_for_span(raw_retry)
                        span.add_event("llm.response", response_event)
                        span.add_event(
                            "llm.parse",
                            {
                                "attempt": 2,
                                "retry": True,
                                "parse_error": str(
                                    parsed_retry.get("parse_error") or ""
                                ),
                                "rca_present": bool(parsed_retry.get("rca")),
                                "fault_category_present": bool(
                                    parsed_retry.get("fault_category")
                                ),
                            },
                        )
                        record_llm_io(
                            {
                                **audit_base,
                                "attempt": 2,
                                "retry": True,
                                "status": "ok",
                                "prompt": prompt,
                                "response": raw_retry,
                                "parse_error": parsed_retry.get("parse_error"),
                                "rca_present": bool(parsed_retry.get("rca")),
                                "fault_category_present": bool(
                                    parsed_retry.get("fault_category")
                                ),
                            }
                        )
                        if parsed_retry.get("rca") and not parsed_retry.get(
                            "parse_error"
                        ):
                            parsed = parsed_retry
                            raw = raw_retry
                            llm_latency_ms = int((time.time() - t_call_start) * 1000)
                            timing_meta["llm_ms"] = llm_latency_ms
                    except LLMClientError as exc:
                        span.add_event(
                            "llm.response_error",
                            {
                                "attempt": 2,
                                "retry": True,
                                "error": _trim_for_span(str(exc), 2000),
                            },
                        )
                        record_llm_io(
                            {
                                **audit_base,
                                "attempt": 2,
                                "retry": True,
                                "status": "error",
                                "error": str(exc),
                                "prompt": prompt,
                            }
                        )
                        # Keep the original failure — don't mask the first error.
                        _log(
                            f"llm.call.error attempt=2 error={_trim_for_span(str(exc), 500)}"
                        )

            root.set_attribute("rca.retry_count", retry_count)
            record_llm_metrics(duration_s=llm_latency_ms / 1000.0, model=target_model)
            timing_meta["total_ms"] = int((time.monotonic() - request_started) * 1000)
            _log(
                f"analysis.done retry_count={retry_count} "
                f"rag_ms={timing_meta['rag_ms']} prompt_ms={timing_meta['prompt_ms']} "
                f"llm_ms={timing_meta['llm_ms']} parse_ms={timing_meta['parse_ms']} "
                f"total_ms={timing_meta['total_ms']}"
            )

            target = rag.get("target") or {}
            analysis = {
                **parsed,
                "rag_mode": rag.get("mode"),
                "prompt_meta": meta,
                "max_new_tokens": max_new_tokens,
                "llm_latency_ms": llm_latency_ms,
                "timing_meta": timing_meta,
                "retry_count": retry_count,
            }
            # FaultCategoryValidator / ServiceLocalizerValidator —
            # deterministic post-processors that may override
            # `fault_category` / `rca` when window-scoped metric hotspots
            # give an unambiguous signal. Always populate `validator_meta`
            # for audit. See analysis/fault_category_validator.py and
            # analysis/service_localizer.py for the rules.
            try:
                features_for_validator = storage.get_run_features(run_id) or {}
                with tracer.start_as_current_span("validator.fault_category") as span:
                    pre_fault = analysis.get("fault_category")
                    analysis = validate_fault_category(
                        analysis,
                        features_for_validator,
                    )
                    vmeta = analysis.get("validator_meta") or {}
                    span.set_attribute("rca.validator.fired", bool(vmeta.get("fired")))
                    if vmeta.get("rule"):
                        span.set_attribute("rca.validator.rule", str(vmeta["rule"]))
                    fault_overrode = bool(vmeta.get("fired")) and (
                        pre_fault != analysis.get("fault_category")
                    )
                    span.set_attribute("rca.validator.override_applied", fault_overrode)
                    if fault_overrode:
                        record_override("fault_category", vmeta.get("rule"))
                with tracer.start_as_current_span(
                    "validator.service_localizer"
                ) as span:
                    pre_rca = analysis.get("rca")
                    analysis = localize_service(
                        analysis,
                        features_for_validator,
                    )
                    loc = (analysis.get("validator_meta") or {}).get("localizer") or {}
                    span.set_attribute("rca.localizer.fired", bool(loc.get("fired")))
                    if loc.get("rule"):
                        span.set_attribute("rca.localizer.rule", str(loc["rule"]))
                    rca_overrode = bool(loc.get("fired")) and (
                        pre_rca != analysis.get("rca")
                    )
                    span.set_attribute("rca.localizer.override_applied", rca_overrode)
                    if rca_overrode:
                        record_override("rca", loc.get("rule"))
            except Exception as exc:  # pragma: no cover - defensive
                analysis["validator_meta"] = {
                    "fired": False,
                    "rule": None,
                    "original_fault_category": analysis.get("fault_category"),
                    "new_fault_category": analysis.get("fault_category"),
                    "evidence": [],
                    "conflict": None,
                    "reason": f"validator error: {exc!s}",
                }

            # ── Decision provenance ──────────────────────────────────────
            # Record which subsystem produced each final field and the
            # evidence behind it — this is what lets us later answer
            # "which data was essential for this decision?".
            vmeta = analysis.get("validator_meta") or {}
            loc = vmeta.get("localizer") or {}
            fault_source = "validator_override" if vmeta.get("fired") else "llm"
            rca_source = "localizer_override" if loc.get("fired") else "llm"
            analysis["decision_provenance"] = {
                "rca": {
                    "source": rca_source,
                    "value": analysis.get("rca"),
                    "llm_original": (
                        loc.get("original_rca")
                        if loc.get("fired")
                        else analysis.get("rca")
                    ),
                    "evidence": loc.get("evidence") or [],
                },
                "fault_category": {
                    "source": fault_source,
                    "value": analysis.get("fault_category"),
                    "llm_original": (
                        vmeta.get("original_fault_category")
                        if vmeta.get("fired")
                        else analysis.get("fault_category")
                    ),
                    "evidence": vmeta.get("evidence") or [],
                },
                "rag": {
                    "mode": rag.get("mode"),
                    "cited": parsed.get("citations") or [],
                    "neighbours": [
                        n.get("run_id") for n in (rag.get("neighbours") or [])
                    ],
                },
            }
            with tracer.start_as_current_span("agent.decision") as span:
                span.set_attribute("gen_ai.operation.name", "decision")
                span.set_attribute("rca.decision.rca.source", rca_source)
                span.set_attribute("rca.decision.fault_category.source", fault_source)
                span.set_attribute("rca.decision.rca.value", str(analysis.get("rca") or ""))
                span.set_attribute(
                    "rca.decision.fault_category.value",
                    str(analysis.get("fault_category") or ""),
                )
                if analysis.get("confidence") is not None:
                    span.set_attribute("rca.decision.confidence", float(analysis["confidence"]))
                span.set_attribute(
                    "rca.decision.citation_count",
                    len(parsed.get("citations") or []),
                )
                span.add_event(
                    "agent.decision.completed",
                    {
                        "rca_source": rca_source,
                        "fault_category_source": fault_source,
                        "validator_fired": bool(vmeta.get("fired")),
                        "localizer_fired": bool(loc.get("fired")),
                    },
                )
            root.set_attribute(GenAI.DECISION_SOURCE_RCA, rca_source)
            root.set_attribute(GenAI.DECISION_SOURCE_FAULT, fault_source)
            if analysis.get("rca"):
                root.set_attribute("rca.final.rca", str(analysis["rca"]))
            if analysis.get("fault_category"):
                root.set_attribute(
                    "rca.final.fault_category", str(analysis["fault_category"])
                )
            if analysis.get("confidence") is not None:
                root.set_attribute(
                    "rca.final.confidence", float(analysis["confidence"])
                )

            with tracer.start_as_current_span("analysis.persist"):
                storage.upsert_llm_analysis(run_id, analysis)
            return {
                "run_id": run_id,
                "heuristic_verdict": target.get("verdict"),
                "analysis": analysis,
                "cached": False,
            }

    @app.get("/api/runs/{run_id}/llm-analysis")
    def get_llm_analysis(run_id: str) -> dict:
        cached = storage.get_llm_analysis(run_id)
        if cached is None:
            raise HTTPException(
                status_code=404, detail="llm analysis not generated yet"
            )
        return {**cached, "cached": True}

    # ── Operator feedback / verdict reconciliation (Phase E) ─────────
    @app.get("/api/verdicts")
    def list_verdicts(
        experiment_id: Optional[str] = None,
        disagreement_only: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        return storage.list_run_verdicts(
            experiment_id=experiment_id,
            disagreement_only=disagreement_only,
            limit=limit,
        )

    @app.get("/api/runs/{run_id}/verdicts")
    def get_verdicts(run_id: str) -> dict:
        v = storage.get_run_verdicts(run_id)
        if v is None:
            raise HTTPException(status_code=404, detail="run_features not found")
        return v

    @app.post("/api/runs/{run_id}/operator-label")
    def set_operator_label(
        run_id: str,
        req: OperatorLabelRequest,
        _: str = Depends(require_roles({"operator", "chaos_engineer", "admin"})),
    ) -> dict:
        try:
            storage.upsert_operator_label(run_id, req.label, by=req.by, note=req.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return storage.get_run_verdicts(run_id) or {"run_id": run_id}

    @app.get("/api/features")
    def list_run_features(
        experiment_id: Optional[str] = None,
        verdict: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        return storage.list_run_features(
            experiment_id=experiment_id, verdict=verdict, limit=limit
        )

    @app.post("/api/runs/{run_id}/manual-conclusion")
    def set_manual_conclusion(
        run_id: str,
        req: ManualConclusionRequest,
        _: str = Depends(require_roles({"operator", "chaos_engineer", "admin"})),
    ) -> dict:
        try:
            run = storage.get_run(run_id)
            run.manual_conclusion = req.conclusion
            storage.update_run(run)
            return {"run_id": run_id, "manual_conclusion": run.manual_conclusion}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/compare/{run_a}/{run_b}")
    def compare_runs(run_a: str, run_b: str) -> dict:
        try:
            a = storage.get_run(run_a)
            b = storage.get_run(run_b)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {
            "run_a": {
                "run_id": a.run_id,
                "status": a.status.value,
                "verdict": a.verdict,
                "timings": a.timings,
                "manual_conclusion": a.manual_conclusion,
            },
            "run_b": {
                "run_id": b.run_id,
                "status": b.status.value,
                "verdict": b.verdict,
                "timings": b.timings,
                "manual_conclusion": b.manual_conclusion,
            },
            "delta": {
                "total_duration_seconds": (
                    (a.timings.get("experiment_total_duration_seconds") or 0)
                    - (b.timings.get("experiment_total_duration_seconds") or 0)
                ),
                "verdict_changed": a.verdict != b.verdict,
            },
        }

    @app.post("/api/governance/kill-switch/enable")
    def enable_kill_switch(_: str = Depends(require_roles({"admin"}))) -> dict:
        kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
        kill_switch_path.write_text("enabled", encoding="utf-8")
        return {"enabled": True, "path": str(kill_switch_path)}

    @app.post("/api/governance/kill-switch/disable")
    def disable_kill_switch(_: str = Depends(require_roles({"admin"}))) -> dict:
        if kill_switch_path.exists():
            kill_switch_path.unlink()
        return {"enabled": False, "path": str(kill_switch_path)}

    litmus_plugin = LitmusChaosPlugin()

    @app.get("/api/providers/chaos/catalog")
    def list_chaos_catalog(sync: bool = False) -> list[dict]:
        """Return known chaos engines.

        Pass ``?sync=true`` to re-fetch from the cluster and update the DB.
        """
        if sync:
            try:
                entries = litmus_plugin.list_catalog()
                storage.upsert_chaos_catalog(entries)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"kubectl sync failed: {exc}",
                ) from exc
        return storage.list_chaos_catalog()

    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    return app
