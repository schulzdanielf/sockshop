"""Read-only retrieval benchmark on frozen A records, not a new RCA evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from experiment.eval.memory_hog.metric_profile_retrieval import (
    METRIC_PROFILE_VERSION,
    PROFILE_METHODS,
    MetricProfileIndex,
    ProfileCandidate,
    fuse_rankings,
)
from experiment.eval.memory_hog.opensearch_experiments import OpenSearchExperimentIndex
from experiment.platform.backend.rca_knowledge import (
    A_TEXT_VERSION,
    OPENSEARCH_RETRIEVER_V5,
    OPENSEARCH_RETRIEVER_V6,
    IncidentA,
    digest,
)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def relevant_rank(ranking: list[str], targets: list[str]) -> int | None:
    return next((i for i, pair_id in enumerate(ranking, 1) if pair_id in targets), None)


def score(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    ranks = [relevant_rank(row["rankings"][name], row["targets"]) for row in rows]
    n = len(rows)
    covered = sum(bool(row["targets"]) for row in rows)
    reciprocal_sum = sum(1 / rank for rank in ranks if rank is not None)
    hits = {k: sum(rank is not None and rank <= k for rank in ranks) for k in (1, 3, 5)}
    return {
        "n": n,
        "covered": covered,
        **{f"hit_at_{k}": hits[k] for k in hits},
        "hit_at_3_covered": hits[3] / covered if covered else None,
        "mrr": reciprocal_sum / n if n else None,
        "mrr_covered": reciprocal_sum / covered if covered else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    names = list(rows[0]["rankings"]) if rows else []
    changes = {}
    for name in names:
        gained, lost, ranks_worsened = [], [], []
        for row in rows:
            baseline = relevant_rank(row["rankings"]["v5"], row["targets"])
            candidate = relevant_rank(row["rankings"][name], row["targets"])
            before = baseline is not None and baseline <= 3
            after = candidate is not None and candidate <= 3
            if after and not before:
                gained.append(row["cell_id"])
            if before and not after:
                lost.append(row["cell_id"])
            if baseline is not None and (candidate is None or candidate > baseline):
                ranks_worsened.append(row["cell_id"])
        changes[name] = {
            "gained_hit3": gained,
            "lost_hit3": lost,
            "worsened_relevant_rank": ranks_worsened,
        }
    return {
        "scores": {name: score(rows, name) for name in names},
        "changes": changes,
        "by_fault": {
            fault: {
                name: score([r for r in rows if r["fault_type"] == fault], name)
                for name in names
            }
            for fault in sorted({r["fault_type"] for r in rows})
        },
        "by_service": {
            service: {
                name: score([r for r in rows if r["service"] == service], name)
                for name in names
            }
            for service in sorted({r["service"] for r in rows})
        },
        "rows": rows,
    }


def audit_inputs(
    training: list[ProfileCandidate], queries: list[ProfileCandidate]
) -> dict[str, Any]:
    def windows(candidate: ProfileCandidate) -> set[tuple[datetime, datetime]]:
        return {
            (
                datetime.fromisoformat(window.start.replace("Z", "+00:00")),
                datetime.fromisoformat(window.end.replace("Z", "+00:00")),
            )
            for fact in candidate.incident.metric_facts
            for window in (fact.baseline_window, fact.fault_window)
        }

    reused_runs = {c.source_run_id for c in training} & {
        c.source_run_id for c in queries
    }
    train_windows = [(c.source_run_id, windows(c)) for c in training]
    test_windows = [(c.source_run_id, windows(c)) for c in queries]
    overlaps = [
        [train_run, test_run]
        for train_run, before in train_windows
        for test_run, after in test_windows
        if any(s1 < e2 and s2 < e1 for s1, e1 in before for s2, e2 in after)
    ]
    if reused_runs or overlaps:
        raise ValueError("Training/test source runs or metric windows overlap")
    hashes_train = {digest(c.incident.model_dump()) for c in training}
    hashes_test = {digest(c.incident.model_dump()) for c in queries}
    return {
        "shared_source_runs": sorted(reused_runs),
        "overlapping_metric_window_run_pairs": overlaps,
        "identical_a_count": len(hashes_train & hashes_test),
        "facts": {
            name: {
                "count": len(facts),
                "partial": sum(f.status == "partial" for f in facts),
                "baseline_sample_min": min(
                    (f.baseline_samples for f in facts), default=None
                ),
                "fault_sample_min": min((f.fault_samples for f in facts), default=None),
            }
            for name, candidates in (("training", training), ("test", queries))
            for facts in [[f for c in candidates for f in c.incident.metric_facts]]
        },
    }


def rank_variants(
    profile: MetricProfileIndex,
    incident: IncidentA,
    lexical: list[str],
    source_run_id: str,
    allowed: set[str],
) -> dict[str, list[str]]:
    rankings = {"v5": lexical}
    for method in PROFILE_METHODS:
        numeric = profile.rank(
            incident,
            source_run_id=source_run_id,
            allowed_pair_ids=allowed,
            method=method,
        )
        rankings[method] = numeric
        for weight in (0.25, 0.5, 0.75):
            rankings[f"{method}_rrf_{weight}"] = fuse_rankings(lexical, numeric, weight)
    for threshold in (3.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0):
        for weight in (0.5, 0.75):
            rankings[f"gated_{threshold}_{weight}"] = profile.rerank(
                lexical,
                incident,
                source_run_id=source_run_id,
                allowed_pair_ids=allowed,
                threshold=threshold,
                numeric_weight=weight,
            )
    for metric in sorted({key[1] for key in profile.features}):
        reduced = incident.model_copy(
            update={
                "metric_facts": [f for f in incident.metric_facts if f.metric != metric]
            }
        )
        rankings[f"gated_without_{metric}"] = profile.rerank(
            lexical, reduced, source_run_id=source_run_id, allowed_pair_ids=allowed
        )
    return rankings


def run(evaluation: Path, url: str) -> dict[str, Any]:
    base = read_object(evaluation / "rag_base.json")
    if digest({k: v for k, v in base.items() if k != "sha256"}) != base.get("sha256"):
        raise ValueError("Frozen corpus checksum mismatch")
    pairs = base["pairs"]
    profile = MetricProfileIndex(
        [
            ProfileCandidate(
                p["pair_id"], p["source_run_id"], IncidentA.model_validate(p["a"])
            )
            for p in pairs
        ]
    )
    pair_ids = {p["pair_id"] for p in pairs}
    eligible_ids = {
        p["pair_id"]
        for p in pairs
        if p["guide_status"] == "procedurally_validated"
        and p["b_procedural_status"] == "passed"
    }
    index = OpenSearchExperimentIndex(
        url, base["sha256"], retriever_version=OPENSEARCH_RETRIEVER_V5
    )
    metadata = index.connect_existing(
        expected_pair_ids=pair_ids, expected_a_text_version=A_TEXT_VERSION
    )
    production_index = OpenSearchExperimentIndex(
        url, base["sha256"], retriever_version=OPENSEARCH_RETRIEVER_V6
    )
    production_index.connect_existing(
        expected_pair_ids=pair_ids, expected_a_text_version=A_TEXT_VERSION
    )
    cohort = read_object(evaluation / "test_cohort.json")["runs"]
    if not cohort:
        raise ValueError("Empty test cohort")
    cell_ids = [entry["cell"]["cell_id"] for entry in cohort]
    if len(cell_ids) != len(set(cell_ids)):
        raise ValueError("Duplicate test cell IDs")
    pools: dict[str, list[dict[str, Any]]] = {"all": [], "eligible": []}
    query_hashes = {}
    queries = []
    for entry in cohort:
        cell = entry["cell"]
        response_path = (
            evaluation / "rag_results" / cell["cell_id"] / "exploratory_rag.json"
        )
        response = read_object(response_path)["response"]
        incident = IncidentA.model_validate(response["a"])
        queries.append(ProfileCandidate(cell["cell_id"], entry["run_id"], incident))
        query_hashes[cell["cell_id"]] = digest(incident.model_dump())
        # Freeze all retrieval outputs before reading any relevance labels.
        outputs = {}
        for pool_name, allowed in (("all", pair_ids), ("eligible", eligible_ids)):
            hits = index.search(
                incident.retrieval_text(),
                None,
                source_run_id=entry["run_id"],
                top_k=len(pair_ids),
                min_score=0,
                include_unvalidated_experimental=False,
                include_unvalidated_procedural_experimental=pool_name == "all",
                query_incident=incident,
                corpus_pairs=pairs,
            )
            outputs[pool_name] = rank_variants(
                profile,
                incident,
                [hit["pair_id"] for hit in hits],
                entry["run_id"],
                allowed,
            )
            production_hits = production_index.search(
                incident.retrieval_text(),
                None,
                source_run_id=entry["run_id"],
                top_k=len(pair_ids),
                min_score=0,
                include_unvalidated_experimental=False,
                include_unvalidated_procedural_experimental=pool_name == "all",
                query_incident=incident,
                corpus_pairs=pairs,
            )
            outputs[pool_name]["production_v6"] = [
                hit["pair_id"] for hit in production_hits
            ]
            if (
                outputs[pool_name]["production_v6"]
                != outputs[pool_name]["gated_10.0_0.75"]
            ):
                raise ValueError(
                    "Integrated v6 rankings differ from the experimental reference"
                )
        targets = {
            p["pair_id"]
            for p in pairs
            if p["source_run_id"] != entry["run_id"]
            and p["labels"]["service"] == cell["service"]
            and p["labels"]["fault_type"] == cell["chaos_type"]
        }
        for pool_name, allowed in (("all", pair_ids), ("eligible", eligible_ids)):
            pools[pool_name].append(
                {
                    "cell_id": cell["cell_id"],
                    "service": cell["service"],
                    "fault_type": cell["chaos_type"],
                    "targets": sorted(targets & allowed),
                    "numeric_strength": profile.strength(incident),
                    "rankings": outputs[pool_name],
                }
            )
    script_files = [
        Path(__file__),
        Path(__file__).with_name("metric_profile_retrieval.py"),
        Path(__file__).with_name("opensearch_experiments.py"),
    ]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evaluation": evaluation.name,
        "version": METRIC_PROFILE_VERSION,
        "base_sha256": base["sha256"],
        "query_a_sha256": query_hashes,
        "implementation_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in script_files
        },
        "index": metadata,
        "candidate_count": len(pair_ids),
        "eligible_count": len(eligible_ids),
        "input_audit": audit_inputs(profile.candidates, queries),
        "mode": "exploratory_retrieval_only_no_new_RCA",
        "limitations": [
            "All test cases were already observed; strategy selection is post-hoc.",
            "No historical MCP collection or new RCA inference was performed.",
            "Relevance is exact service/fault matching, not causal applicability.",
            "Missing metric facts are zero-filled for similarity, not negative evidence.",
            "Only positive deltas contribute to the gated numeric route.",
            "min_score=0 and full rankings; runtime context packing is not evaluated.",
            "The corpus top-delta projection is not full telemetry coverage.",
            "Leave-one-metric-out ablations remove facts only from the numeric route; BM25 is unchanged.",
        ],
        "scale_fit": [
            {"metric": k[0], "unit": k[1], "statistic": k[2], "scale": v}
            for k, v in sorted(profile.scales.items())
        ],
        "pools": {pool: summarize(rows) for pool, rows in pools.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:9200")
    args = parser.parse_args()
    result = run(args.evaluation, args.url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    for pool, summary in result["pools"].items():
        for name in (
            "v5",
            "positive",
            "positive_rrf_0.75",
            "gated_10.0_0.75",
            "production_v6",
        ):
            row = summary["scores"][name]
            changes = summary["changes"][name]
            print(
                pool,
                name,
                f"Hit@3={row['hit_at_3']}/{row['covered']}",
                f"MRR={row['mrr']:.4f}",
                f"gains={len(changes['gained_hit3'])}",
                f"losses={len(changes['lost_hit3'])}",
            )


if __name__ == "__main__":
    main()
