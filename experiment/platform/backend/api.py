from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .adapters.noop_notifier import NoopNotificationAdapter
from .engine import OrchestratorEngine
from .analysis import (
    rank_similar_runs,
    rank_by_embedding,
    rank_hybrid,
    get_embedding_provider,
    vector_to_blob,
    blob_to_vector,
    assemble_prompt,
    call_llm,
    parse_verdict_response,
    llm_health,
    LLMClientError,
    list_system_cards,
    system_card_meta,
    render_system_card,
)
import numpy as np
from .models import (
    ApproveRunRequest,
    CreateExperimentRequest,
    ManualConclusionRequest,
    OperatorLabelRequest,
    StartRunRequest,
    StopRunRequest,
)
from .plugins.litmus_plugin import LitmusChaosPlugin
from .plugins.locust_plugin import LocustLoadPlugin
from .plugins.mcp_metrics_plugin import MCPPrometheusMetricsPlugin
from .plugins.mcp_traces_plugin import MCPTempoTracesPlugin
from .storage.sqlite_storage import SqliteStorage
from .security import require_roles


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
                    row["graph"] = (row.get("features") or {}).get("propagation_graph") or {}
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
                        c.get("service") for c in (target_graph.get("cascade_order") or [])
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
                            "cascade_overlap_services": n.get("cascade_overlap_services", []),
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
    def get_rag_context(
        run_id: str, limit: int = 3, mode: str = "embedding"
    ) -> dict:
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
                    # NOTE: do NOT filter by chaos_type — that would act as an
                    # oracle, only returning neighbours of the same fault type
                    # as the target and trivialising the classification task.
                    limit=1000,
                    include_features=True,
                )
                candidates = []
                for row in rows:
                    try:
                        row["vector"] = blob_to_vector(row["blob"], row["dim"])
                    except ValueError:
                        continue
                    row["graph"] = (row.get("features") or {}).get("propagation_graph") or {}
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
                            "cascade_overlap_services": n.get("cascade_overlap_services", []),
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
                # NOTE: do NOT filter by chaos_type — see hybrid branch above.
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
                # NOTE: do NOT filter by chaos_type — see hybrid branch above.
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
    ) -> dict:
        """Assemble RAG prompt, call Qwen, persist parsed verdict."""
        if not force:
            cached = storage.get_llm_analysis(run_id)
            if cached is not None:
                return {**cached, "cached": True}

        rag = _build_rag_context(run_id, limit, mode)
        prompt, meta = assemble_prompt(
            rag,
            budget_tokens=budget_tokens,
            max_neighbours=limit,
            system_id=system_id,
        )

        t_call_start = time.time()
        try:
            raw = call_llm(prompt, max_new_tokens=max_new_tokens)
        except LLMClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        llm_latency_ms = int((time.time() - t_call_start) * 1000)
        parsed = parse_verdict_response(raw)

        target = rag.get("target") or {}
        analysis = {
            **parsed,
            "rag_mode": rag.get("mode"),
            "prompt_meta": meta,
            "max_new_tokens": max_new_tokens,
            "llm_latency_ms": llm_latency_ms,
        }
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
            raise HTTPException(status_code=404, detail="llm analysis not generated yet")
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
            storage.upsert_operator_label(
                run_id, req.label, by=req.by, note=req.note
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return storage.get_run_verdicts(run_id) or {"run_id": run_id}

    @app.get("/api/features")
    def list_run_features(
        experiment_id: Optional[str] = None,
        verdict: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        return storage.list_run_features(experiment_id=experiment_id, verdict=verdict, limit=limit)

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
