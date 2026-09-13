"""FastAPI application factory and HTTP routing layer.

Exposes :func:`create_app`, which wires the orchestrator engine, storage
and provider plugins into the REST API used to drive chaos experiments.
This is the inbound adapter of the hexagonal architecture: it translates
HTTP requests into domain operations and serialises domain state back out.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from opentelemetry.trace import Status, StatusCode

from .adapters.noop_notifier import NoopNotificationAdapter
from .analysis import (
    DEFAULT_LLM_MODEL,
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
from .engine import OrchestratorEngine
from .models import (
    ApproveRunRequest,
    CreateExperimentRequest,
    ManualConclusionRequest,
    OperatorLabelRequest,
    StartRunRequest,
    StopRunRequest,
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
from .plugins.mcp_metrics_plugin import MCPPrometheusMetricsPlugin
from .plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from .security import require_roles
from .storage.sqlite_storage import SqliteStorage


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

    engine = OrchestratorEngine(
        storage=storage,
        chaos=LitmusChaosPlugin(),
        load=LocustLoadPlugin(),
        metrics=MCPPrometheusMetricsPlugin(),
        traces=MCPTempoTracesPlugin(),
        notifier=NoopNotificationAdapter(),
        kill_switch_path=kill_switch_path,
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

    @app.get("/api/runs/{run_id}/features")
    def get_run_features(run_id: str) -> dict:
        features = storage.get_run_features(run_id)
        if features is None:
            raise HTTPException(status_code=404, detail="features not found")
        return features

    @app.get("/api/runs/{run_id}/summary")
    def get_run_summary(run_id: str) -> dict:
        summary = storage.get_run_summary(run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="summary not found")
        return summary

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
        target_experiment_version = target.get("experiment_version")

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
                    experiment_version=target_experiment_version,
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
                experiment_version=target_experiment_version,
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
                experiment_version=target_experiment_version,
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
                    experiment_id=target.get("experiment_id"),
                    experiment_version=target.get("experiment_version"),
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
                experiment_id=target.get("experiment_id"),
                experiment_version=target.get("experiment_version"),
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
                experiment_id=target.get("experiment_id"),
                experiment_version=target.get("experiment_version"),
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

        # Resolve LLM target URL and model name for this request.
        # Fall back to environment defaults (Qwen local) if not explicitly passed.
        prov = (provider or "").strip().lower()
        if prov in {"gemini", "gemini-flash", "gemini-1.5-flash", "gemini-2.5-flash", "google"}:
            target_model = model or "gemini-2.5-flash"
            target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"
            target_api_style = "gemini"
        elif prov in {"gpt", "gpt-4o-mini", "openai", "github"}:
            env_url = os.environ.get("LLM_URL")
            if env_url and "localhost" not in env_url and "127.0.0.1" not in env_url:
                target_url = env_url
            else:
                target_url = "https://api.openai.com/v1/chat/completions"
            target_model = model or os.environ.get("LLM_MODEL") or "gpt-4o-mini"
            target_api_style = "openai"
        elif prov in {"qwen", "local"}:
            target_url = "http://localhost:8001/generate"
            target_model = model or "qwen-14b"
            target_api_style = "legacy"
        elif prov:
            target_url = prov  # custom URL passed directly
            target_model = model or DEFAULT_LLM_MODEL
            target_api_style = None
        else:
            target_url = DEFAULT_LLM_URL
            target_model = model or DEFAULT_LLM_MODEL
            target_api_style = None

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
                rag = _build_rag_context(run_id, limit, mode)
                neighbours = rag.get("neighbours") or []
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
                if capture_content():
                    span.set_attribute(GenAI.PROMPT, prompt)

            # ── LLM call ─────────────────────────────────────────────────
            t_call_start = time.time()
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
                span.add_event(
                    "llm.request",
                    {
                        "attempt": 1,
                        "retry": False,
                        "prompt": _trim_for_span(prompt),
                    },
                )
                try:
                    raw = call_llm(
                        prompt,
                        max_new_tokens=max_new_tokens,
                        url=target_url,
                        model=target_model,
                        api_style=target_api_style,
                        api_key=api_key,
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
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                if capture_content():
                    span.set_attribute(GenAI.COMPLETION, raw)
                span.add_event(
                    "llm.response",
                    {
                        "attempt": 1,
                        "retry": False,
                        "response": _trim_for_span(raw),
                    },
                )

            llm_latency_ms = int((time.time() - t_call_start) * 1000)
            with tracer.start_as_current_span("response.parse") as span:
                parsed = parse_verdict_response(raw)
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
                    span.add_event(
                        "llm.request",
                        {
                            "attempt": 2,
                            "retry": True,
                            "prompt": _trim_for_span(prompt),
                        },
                    )
                    try:
                        raw_retry = call_llm(
                            prompt,
                            max_new_tokens=max_new_tokens,
                            url=target_url,
                            model=target_model,
                            api_style=target_api_style,
                            api_key=api_key,
                        )
                        parsed_retry = parse_verdict_response(raw_retry)
                        span.add_event(
                            "llm.response",
                            {
                                "attempt": 2,
                                "retry": True,
                                "response": _trim_for_span(raw_retry),
                            },
                        )
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

            root.set_attribute("rca.retry_count", retry_count)
            record_llm_metrics(duration_s=llm_latency_ms / 1000.0, model=target_model)

            target = rag.get("target") or {}
            analysis = {
                **parsed,
                "rag_mode": rag.get("mode"),
                "prompt_meta": meta,
                "max_new_tokens": max_new_tokens,
                "llm_latency_ms": llm_latency_ms,
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
