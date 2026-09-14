"""SQLite-backed implementation of the storage port.

``SqliteStorage`` persists experiments, versions, run records and domain
events to a local SQLite database. It is the outbound persistence adapter
fulfilling :class:`ports.StoragePort`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..domain import DomainEvent, ExperimentVersion, RunRecord, RunStatus, utc_now_iso


class SqliteStorage:
    def __init__(self, db_path: Path, object_store_root: Path):
        self.db_path = db_path
        self.object_store_root = object_store_root
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.object_store_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            # Migrate chaos_catalog if it exists with old PK schema
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chaos_catalog'"
            ).fetchone()
            if row and "PRIMARY KEY (engine_name" in (row["sql"] or ""):
                conn.execute("DROP TABLE chaos_catalog")
                conn.commit()

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    PRIMARY KEY (experiment_id, version)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    experiment_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    initiated_by TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    verdict TEXT,
                    timings_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    manual_conclusion TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chaos_catalog (
                    workflow_name TEXT NOT NULL,
                    engine_namespace TEXT NOT NULL,
                    engine_name TEXT,
                    app_namespace TEXT,
                    app_label TEXT,
                    experiment_types_json TEXT NOT NULL,
                    engine_state TEXT,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (workflow_name, engine_namespace)
                );

                CREATE TABLE IF NOT EXISTS run_features (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    experiment_version INTEGER NOT NULL DEFAULT 1,
                    chaos_type TEXT,
                    verdict TEXT,
                    slo_violation_count INTEGER NOT NULL DEFAULT 0,
                    recovery_time_seconds REAL,
                    affected_services_json TEXT NOT NULL DEFAULT '[]',
                    features_json TEXT NOT NULL,
                    summary_text TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    embedding_blob BLOB,
                    embedding_provider TEXT,
                    embedding_dim INTEGER,
                    llm_analysis_json TEXT,
                    llm_analysis_at TEXT,
                    operator_label TEXT,
                    operator_label_at TEXT,
                    operator_label_by TEXT,
                    operator_note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_run_features_experiment
                    ON run_features(experiment_id);
                CREATE INDEX IF NOT EXISTS idx_run_features_verdict
                    ON run_features(verdict);
                CREATE INDEX IF NOT EXISTS idx_run_features_chaos
                    ON run_features(chaos_type);
                """
            )

            # Idempotent migrations for older DBs missing summary_text/tags_json
            existing_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(run_features)").fetchall()
            }
            if "summary_text" not in existing_cols:
                conn.execute("ALTER TABLE run_features ADD COLUMN summary_text TEXT")
            if "tags_json" not in existing_cols:
                conn.execute(
                    "ALTER TABLE run_features ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "embedding_blob" not in existing_cols:
                conn.execute("ALTER TABLE run_features ADD COLUMN embedding_blob BLOB")
            if "embedding_provider" not in existing_cols:
                conn.execute(
                    "ALTER TABLE run_features ADD COLUMN embedding_provider TEXT"
                )
            if "embedding_dim" not in existing_cols:
                conn.execute(
                    "ALTER TABLE run_features ADD COLUMN embedding_dim INTEGER"
                )
            if "llm_analysis_json" not in existing_cols:
                conn.execute(
                    "ALTER TABLE run_features ADD COLUMN llm_analysis_json TEXT"
                )
            if "llm_analysis_at" not in existing_cols:
                conn.execute("ALTER TABLE run_features ADD COLUMN llm_analysis_at TEXT")
            for col, ddl in (
                ("operator_label", "TEXT"),
                ("operator_label_at", "TEXT"),
                ("operator_label_by", "TEXT"),
                ("operator_note", "TEXT"),
            ):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE run_features ADD COLUMN {col} {ddl}")
            # is_training flag splits the corpus: only is_training=1 rows are
            # eligible as RAG neighbours. Test rows (is_training=0) stay
            # queryable as targets but never leak into retrieval results.
            # Default 1 preserves backward compatibility for pre-existing runs;
            # the backfill script flips bootstrap test rows to 0.
            if "is_training" not in existing_cols:
                conn.execute(
                    "ALTER TABLE run_features ADD COLUMN is_training INTEGER NOT NULL DEFAULT 1"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_run_features_is_training "
                    "ON run_features(is_training)"
                )
            if "experiment_version" not in existing_cols:
                conn.execute(
                    "ALTER TABLE run_features ADD COLUMN experiment_version INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_features_experiment_version "
                "ON run_features(experiment_id, experiment_version)"
            )
            conn.commit()

    def create_experiment(
        self, created_by: str, spec: Dict[str, Any]
    ) -> ExperimentVersion:
        experiment = spec.get("experiment", {})
        experiment_id = experiment.get("id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError("experiment.id is required")

        schema_version = str(spec.get("schema_version", "1.0.0"))
        created_at = utc_now_iso()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS last_version FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            version = int(row["last_version"]) + 1
            conn.execute(
                """
                INSERT INTO experiments (experiment_id, version, schema_version, created_at, created_by, spec_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    version,
                    schema_version,
                    created_at,
                    created_by,
                    json.dumps(spec),
                ),
            )

        return ExperimentVersion(
            experiment_id=experiment_id,
            version=version,
            schema_version=schema_version,
            created_at=created_at,
            created_by=created_by,
            spec=spec,
        )

    def list_experiments(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT e1.experiment_id, e1.version, e1.created_at, e1.created_by
                FROM experiments e1
                INNER JOIN (
                    SELECT experiment_id, MAX(version) AS max_version
                    FROM experiments
                    GROUP BY experiment_id
                ) e2
                ON e1.experiment_id = e2.experiment_id AND e1.version = e2.max_version
                ORDER BY e1.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_experiment(
        self, experiment_id: str, version: Optional[int] = None
    ) -> ExperimentVersion:
        with self._conn() as conn:
            if version is None:
                row = conn.execute(
                    """
                    SELECT * FROM experiments
                    WHERE experiment_id = ?
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (experiment_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM experiments WHERE experiment_id = ? AND version = ?",
                    (experiment_id, version),
                ).fetchone()

        if not row:
            raise KeyError(
                f"Experiment not found: {experiment_id} v{version or 'latest'}"
            )

        return ExperimentVersion(
            experiment_id=row["experiment_id"],
            version=int(row["version"]),
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            spec=json.loads(row["spec_json"]),
        )

    def create_run(
        self, run: RunRecord, idempotency_key: Optional[str] = None
    ) -> RunRecord:
        with self._conn() as conn:
            if idempotency_key:
                row = conn.execute(
                    "SELECT run_id FROM run_idempotency WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return self.get_run(row["run_id"])

            conn.execute(
                """
                INSERT INTO runs (
                    run_id, experiment_id, experiment_version, status, initiated_by,
                    started_at, ended_at, verdict, timings_json, summary_json, manual_conclusion
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.experiment_id,
                    run.experiment_version,
                    run.status.value,
                    run.initiated_by,
                    run.started_at,
                    run.ended_at,
                    run.verdict,
                    json.dumps(run.timings),
                    json.dumps(run.summary),
                    run.manual_conclusion,
                ),
            )
            if idempotency_key:
                conn.execute(
                    "INSERT INTO run_idempotency (idempotency_key, run_id, created_at) VALUES (?, ?, ?)",
                    (idempotency_key, run.run_id, utc_now_iso()),
                )
        return run

    def get_run(self, run_id: str) -> RunRecord:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Run not found: {run_id}")

        return RunRecord(
            run_id=row["run_id"],
            experiment_id=row["experiment_id"],
            experiment_version=int(row["experiment_version"]),
            status=RunStatus(row["status"]),
            initiated_by=row["initiated_by"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            verdict=row["verdict"],
            timings=json.loads(row["timings_json"] or "{}"),
            summary=json.loads(row["summary_json"] or "{}"),
            manual_conclusion=row["manual_conclusion"],
        )

    def list_runs(self, experiment_id: Optional[str] = None) -> List[RunRecord]:
        with self._conn() as conn:
            if experiment_id:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE experiment_id = ? ORDER BY started_at DESC",
                    (experiment_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC"
                ).fetchall()
        return [
            RunRecord(
                run_id=row["run_id"],
                experiment_id=row["experiment_id"],
                experiment_version=int(row["experiment_version"]),
                status=RunStatus(row["status"]),
                initiated_by=row["initiated_by"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                verdict=row["verdict"],
                timings=json.loads(row["timings_json"] or "{}"),
                summary=json.loads(row["summary_json"] or "{}"),
                manual_conclusion=row["manual_conclusion"],
            )
            for row in rows
        ]

    def update_run(self, run: RunRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, ended_at = ?, verdict = ?, timings_json = ?, summary_json = ?, manual_conclusion = ?
                WHERE run_id = ?
                """,
                (
                    run.status.value,
                    run.ended_at,
                    run.verdict,
                    json.dumps(run.timings),
                    json.dumps(run.summary),
                    run.manual_conclusion,
                    run.run_id,
                ),
            )

    def append_event(self, event: DomainEvent) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (run_id, event_type, ts, payload_json) VALUES (?, ?, ?, ?)",
                (event.run_id, event.event_type, event.ts, json.dumps(event.payload)),
            )

    def list_events(self, run_id: str) -> List[DomainEvent]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id, event_type, ts, payload_json FROM events WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        return [
            DomainEvent(
                run_id=row["run_id"],
                event_type=row["event_type"],
                ts=row["ts"],
                payload=json.loads(row["payload_json"] or "{}"),
            )
            for row in rows
        ]

    def save_artifact(self, run_id: str, name: str, content: Dict[str, Any]) -> str:
        run_dir = self.object_store_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = run_dir / f"{name}.json"
        artifact_path.write_text(json.dumps(content, indent=2), encoding="utf-8")

        rel_path = str(artifact_path)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO artifacts (run_id, name, path, created_at) VALUES (?, ?, ?, ?)",
                (run_id, name, rel_path, utc_now_iso()),
            )
        return rel_path

    def upsert_chaos_catalog(self, entries: List[Dict[str, Any]]) -> int:
        """Upsert chaos engine entries into the catalog. Returns count upserted."""
        now = utc_now_iso()
        with self._conn() as conn:
            for entry in entries:
                conn.execute(
                    """
                    INSERT INTO chaos_catalog (
                            workflow_name, engine_namespace, engine_name,
                        app_namespace, app_label, experiment_types_json,
                        engine_state, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(workflow_name, engine_namespace) DO UPDATE SET
                            engine_name=excluded.engine_name,
                        app_namespace=excluded.app_namespace,
                        app_label=excluded.app_label,
                        experiment_types_json=excluded.experiment_types_json,
                        engine_state=excluded.engine_state,
                        synced_at=excluded.synced_at
                    """,
                    (
                        entry.get("workflow_name"),
                        entry.get("engine_namespace"),
                        entry.get("engine_name"),
                        entry.get("app_namespace"),
                        entry.get("app_label"),
                        json.dumps(entry.get("experiment_types", [])),
                        entry.get("engine_state"),
                        now,
                    ),
                )
        return len(entries)

    def list_chaos_catalog(self) -> List[Dict[str, Any]]:
        """Return all chaos catalog entries ordered by namespace and name."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT engine_name, engine_namespace, workflow_name,
                       app_namespace, app_label, experiment_types_json,
                       engine_state, synced_at
                FROM chaos_catalog
                ORDER BY engine_namespace, engine_name
                """
            ).fetchall()
        return [
            {
                "engine_name": row["engine_name"],
                "engine_namespace": row["engine_namespace"],
                "workflow_name": row["workflow_name"],
                "app_namespace": row["app_namespace"],
                "app_label": row["app_label"],
                "experiment_types": json.loads(row["experiment_types_json"] or "[]"),
                "engine_state": row["engine_state"],
                "synced_at": row["synced_at"],
            }
            for row in rows
        ]

    def upsert_run_features(
        self,
        run_id: str,
        experiment_id: str,
        features: Dict[str, Any],
        experiment_version: Optional[int] = None,
    ) -> None:
        """Persist L1 features for a run. Idempotent.

        ``features['is_training']`` (bool) controls whether this row is
        eligible as a RAG neighbour. Defaults to True when unset, matching
        legacy behaviour.

        ``experiment_version`` isolates retrieval to the same campaign revision.
        """
        affected = features.get("affected_services", []) or []
        summary_text = features.get("summary_text")
        tags = features.get("tags") or []
        is_training = 1 if features.get("is_training", True) else 0
        version = (
            int(experiment_version)
            if experiment_version is not None
            else int(features.get("experiment_version", 1))
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO run_features (
                    run_id, experiment_id, experiment_version, chaos_type, verdict,
                    slo_violation_count, recovery_time_seconds,
                    affected_services_json, features_json,
                    summary_text, tags_json, is_training, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    experiment_id=excluded.experiment_id,
                    experiment_version=excluded.experiment_version,
                    chaos_type=excluded.chaos_type,
                    verdict=excluded.verdict,
                    slo_violation_count=excluded.slo_violation_count,
                    recovery_time_seconds=excluded.recovery_time_seconds,
                    affected_services_json=excluded.affected_services_json,
                    features_json=excluded.features_json,
                    summary_text=excluded.summary_text,
                    tags_json=excluded.tags_json,
                    is_training=excluded.is_training
                """,
                (
                    run_id,
                    experiment_id,
                    version,
                    features.get("chaos_type"),
                    features.get("verdict"),
                    int(len(features.get("slo_violations", []) or [])),
                    features.get("recovery_time_seconds"),
                    json.dumps(affected),
                    json.dumps(features),
                    summary_text,
                    json.dumps(tags),
                    is_training,
                    utc_now_iso(),
                ),
            )

    def get_run_features(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT features_json FROM run_features WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["features_json"])

    def get_run_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT run_id, experiment_id, experiment_version, chaos_type, verdict,
                       summary_text, tags_json, created_at
                FROM run_features WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "experiment_id": row["experiment_id"],
            "experiment_version": row["experiment_version"],
            "chaos_type": row["chaos_type"],
            "verdict": row["verdict"],
            "summary_text": row["summary_text"] or "",
            "tags": json.loads(row["tags_json"] or "[]"),
            "created_at": row["created_at"],
        }

    def list_run_summaries(
        self,
        *,
        experiment_id: Optional[str] = None,
        experiment_version: Optional[int] = None,
        verdict: Optional[str] = None,
        chaos_type: Optional[str] = None,
        is_training_only: bool = True,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return lightweight summary records (tags + summary_text).

        ``is_training_only`` (default True) restricts the result set to rows
        marked as training corpus. Callers that genuinely need test rows
        (e.g. evaluation tooling) must opt out explicitly.

        ``experiment_version`` keeps retrieval in the same campaign revision.
        """
        where: List[str] = ["summary_text IS NOT NULL"]
        params: List[Any] = []
        if experiment_id:
            where.append("experiment_id = ?")
            params.append(experiment_id)
        if experiment_version is not None:
            where.append("experiment_version = ?")
            params.append(int(experiment_version))
        if verdict:
            where.append("verdict = ?")
            params.append(verdict)
        if chaos_type:
            where.append("chaos_type = ?")
            params.append(chaos_type)
        if is_training_only:
            where.append("is_training = 1")
        clause = "WHERE " + " AND ".join(where)
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, experiment_id, experiment_version, chaos_type, verdict,
                       summary_text, tags_json, created_at
                FROM run_features
                {clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "experiment_id": r["experiment_id"],
                "experiment_version": r["experiment_version"],
                "chaos_type": r["chaos_type"],
                "verdict": r["verdict"],
                "summary_text": r["summary_text"] or "",
                "tags": json.loads(r["tags_json"] or "[]"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_run_features(
        self,
        experiment_id: Optional[str] = None,
        verdict: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        if experiment_id:
            where.append("experiment_id = ?")
            params.append(experiment_id)
        if verdict:
            where.append("verdict = ?")
            params.append(verdict)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, experiment_id, chaos_type, verdict,
                       slo_violation_count, recovery_time_seconds,
                       affected_services_json, created_at
                FROM run_features
                {clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "experiment_id": r["experiment_id"],
                "chaos_type": r["chaos_type"],
                "verdict": r["verdict"],
                "slo_violation_count": r["slo_violation_count"],
                "recovery_time_seconds": r["recovery_time_seconds"],
                "affected_services": json.loads(r["affected_services_json"] or "[]"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ── Embeddings (Phase C) ─────────────────────────────────────────────
    def upsert_run_embedding(
        self,
        run_id: str,
        provider: str,
        dim: int,
        blob: bytes,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE run_features
                SET embedding_blob = ?,
                    embedding_provider = ?,
                    embedding_dim = ?
                WHERE run_id = ?
                """,
                (blob, provider, int(dim), run_id),
            )

    def get_run_embedding(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT embedding_blob, embedding_provider, embedding_dim
                FROM run_features WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if not row or row["embedding_blob"] is None:
            return None
        return {
            "provider": row["embedding_provider"],
            "dim": row["embedding_dim"],
            "blob": bytes(row["embedding_blob"]),
        }

    def iter_run_embeddings(
        self,
        *,
        provider: Optional[str] = None,
        chaos_type: Optional[str] = None,
        experiment_id: Optional[str] = None,
        experiment_version: Optional[int] = None,
        is_training_only: bool = True,
        limit: int = 1000,
        include_features: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return records with embedding blob + metadata for ranking.

        ``include_features`` adds the full ``features_json`` payload so
        callers (e.g. the hybrid trace-aware ranker in Phase F1) can
        retrieve the propagation graph without a second query.

        ``is_training_only`` (default True) restricts the result set to
        rows flagged as training corpus, preventing test-set self-leakage
        through embedding similarity.

        ``experiment_id`` and ``experiment_version`` narrow candidates to the
        same campaign revision, avoiding cross-campaign retrieval.
        """
        where: List[str] = ["embedding_blob IS NOT NULL"]
        params: List[Any] = []
        if provider:
            where.append("embedding_provider = ?")
            params.append(provider)
        if chaos_type:
            where.append("chaos_type = ?")
            params.append(chaos_type)
        if experiment_id:
            where.append("experiment_id = ?")
            params.append(experiment_id)
        if experiment_version is not None:
            where.append("experiment_version = ?")
            params.append(int(experiment_version))
        if is_training_only:
            where.append("is_training = 1")
        clause = "WHERE " + " AND ".join(where)
        params.append(int(limit))
        extra_cols = ", features_json" if include_features else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, experiment_id, experiment_version, chaos_type, verdict,
                       summary_text, tags_json, created_at,
                       embedding_blob, embedding_provider, embedding_dim{extra_cols}
                FROM run_features
                {clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            item: Dict[str, Any] = {
                "run_id": r["run_id"],
                "experiment_id": r["experiment_id"],
                "experiment_version": r["experiment_version"],
                "chaos_type": r["chaos_type"],
                "verdict": r["verdict"],
                "summary_text": r["summary_text"] or "",
                "tags": json.loads(r["tags_json"] or "[]"),
                "created_at": r["created_at"],
                "provider": r["embedding_provider"],
                "dim": r["embedding_dim"],
                "blob": bytes(r["embedding_blob"]),
            }
            if include_features:
                try:
                    item["features"] = json.loads(r["features_json"] or "{}")
                except (TypeError, ValueError):
                    item["features"] = {}
            out.append(item)
        return out

    def list_runs_without_embedding(
        self,
        *,
        provider: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Runs that have a summary but no/stale embedding for ``provider``."""
        params: List[Any] = []
        if provider:
            where = (
                "summary_text IS NOT NULL AND "
                "(embedding_blob IS NULL OR embedding_provider != ?)"
            )
            params.append(provider)
        else:
            where = "summary_text IS NOT NULL AND embedding_blob IS NULL"
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, summary_text
                FROM run_features
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {"run_id": r["run_id"], "summary_text": r["summary_text"] or ""}
            for r in rows
        ]

    # ── LLM analysis ─────────────────────────────────────────────────
    def upsert_llm_analysis(self, run_id: str, analysis: Dict[str, Any]) -> None:
        """Persist the parsed LLM verdict alongside the run features."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE run_features
                SET llm_analysis_json = ?, llm_analysis_at = ?
                WHERE run_id = ?
                """,
                (json.dumps(analysis), utc_now_iso(), run_id),
            )

    def get_llm_analysis(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT llm_analysis_json, llm_analysis_at, verdict
                FROM run_features
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if not row or not row["llm_analysis_json"]:
            return None
        try:
            analysis = json.loads(row["llm_analysis_json"])
        except json.JSONDecodeError:
            return None
        return {
            "run_id": run_id,
            "heuristic_verdict": row["verdict"],
            "generated_at": row["llm_analysis_at"],
            "analysis": analysis,
        }

    # ── Operator labels (Phase E) ────────────────────────────────────
    def upsert_operator_label(
        self,
        run_id: str,
        label: str,
        *,
        by: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            updated = conn.execute(
                """
                UPDATE run_features
                SET operator_label = ?,
                    operator_label_at = ?,
                    operator_label_by = ?,
                    operator_note = ?
                WHERE run_id = ?
                """,
                (label, utc_now_iso(), by, note, run_id),
            ).rowcount
        if not updated:
            raise KeyError(f"run_features not found for run_id={run_id}")

    def _row_to_verdicts(self, row: sqlite3.Row) -> Dict[str, Any]:
        llm_verdict = None
        llm_confidence = None
        llm_parse_error = None
        if row["llm_analysis_json"]:
            try:
                analysis = json.loads(row["llm_analysis_json"])
                llm_verdict = analysis.get("verdict")
                llm_confidence = analysis.get("confidence")
                llm_parse_error = analysis.get("parse_error")
            except json.JSONDecodeError:
                llm_parse_error = "stored json invalid"
        heuristic = row["verdict"]
        operator = row["operator_label"]
        verdicts = [v for v in (heuristic, llm_verdict, operator) if v]
        agreement = len(set(verdicts)) <= 1 if verdicts else None
        disagreement = None if agreement is None else (not agreement)
        return {
            "run_id": row["run_id"],
            "experiment_id": row["experiment_id"],
            "chaos_type": row["chaos_type"],
            "heuristic_verdict": heuristic,
            "llm_verdict": llm_verdict,
            "llm_confidence": llm_confidence,
            "llm_parse_error": llm_parse_error,
            "llm_analysis_at": row["llm_analysis_at"],
            "operator_label": operator,
            "operator_label_at": row["operator_label_at"],
            "operator_label_by": row["operator_label_by"],
            "operator_note": row["operator_note"],
            "agreement": agreement,
            "disagreement": disagreement,
            "created_at": row["created_at"],
        }

    def get_run_verdicts(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT run_id, experiment_id, chaos_type, verdict,
                       llm_analysis_json, llm_analysis_at,
                       operator_label, operator_label_at,
                       operator_label_by, operator_note,
                       created_at
                FROM run_features WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_verdicts(row)

    def list_run_verdicts(
        self,
        *,
        experiment_id: Optional[str] = None,
        disagreement_only: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        if experiment_id:
            where.append("experiment_id = ?")
            params.append(experiment_id)
        sql = (
            "SELECT run_id, experiment_id, chaos_type, verdict, "
            "       llm_analysis_json, llm_analysis_at, "
            "       operator_label, operator_label_at, "
            "       operator_label_by, operator_note, created_at "
            "FROM run_features"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = [self._row_to_verdicts(r) for r in rows]
        if disagreement_only:
            out = [v for v in out if v["disagreement"] is True]
        return out
