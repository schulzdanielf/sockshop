"""Retry incompatible exploratory RAG diagnostics without submitting experiments."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from experiment.eval.memory_hog.rag_campaign import _score, options_for
from experiment.eval.memory_hog.retroactive_b import OBJECT_STORE
from experiment.platform.backend.analysis.llm_client import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_URL,
    call_llm_tool_messages,
    llm_health,
)
from experiment.platform.backend.plugins.mcp_client import MCPToolClient
from experiment.platform.backend.rca_knowledge import (
    KnowledgeOptions,
    KnowledgeStore,
    digest,
    investigate,
    write_once,
)

RETRY_VERSION = "exploratory-rag-model-retry-v1"
METHODS = ("original", "exploratory_control", "exploratory_rag")
MISMATCH = "exploratory base hash/version/provider mismatch"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def failed_cases(campaign: Path, evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    for row in evaluation["per_run"]:
        if row["run_status"] != "completed":
            continue
        path = campaign / "rag_results" / row["cell_id"] / "exploratory_rag.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["run_id"] != row["run_id"]:
            raise ValueError("saved RAG diagnostic belongs to a different run")
        response = record["response"]
        if response.get("status") == "error" and MISMATCH in response.get("error", ""):
            selected.append(row)
    if not selected:
        raise ValueError("no completed runs with the targeted compatibility error")
    if len({row["run_id"] for row in selected}) != len(selected):
        raise ValueError("duplicate retry run ID")
    return selected


def derive_base(
    source: dict[str, Any], expected_hash: str, store: KnowledgeStore
) -> dict[str, Any]:
    payload = {key: value for key, value in source.items() if key != "sha256"}
    if source["sha256"] != expected_hash or digest(payload) != expected_hash:
        raise ValueError("source exploratory base integrity mismatch")
    if source["model"] == DEFAULT_LLM_MODEL:
        raise ValueError("source model already matches; this is not a model migration")
    derived = copy.deepcopy(payload)
    derived["model"] = DEFAULT_LLM_MODEL
    result = {**derived, "sha256": digest(derived)}
    with store.locked():
        write_once(store.directory / "exploratory_base.json", result)
        # All existing skill, provider, pair-integrity and model guards still apply.
        return store.load_exploratory_base()


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        method: {
            "denominator": len(rows),
            **{
                key: sum(row["methods"][method][key] for row in rows)
                for key in (
                    "classification_complete",
                    "service_correct",
                    "fault_type_correct",
                    "joint_correct",
                    "confirmed",
                    "inconclusive",
                    "unsupported_confirmation",
                    "incorrect_confirmation",
                )
            },
            "statuses": dict(Counter(row["methods"][method]["status"] for row in rows)),
        }
        for method in METHODS
    }


def evaluate_retries(
    original: dict[str, Any],
    selected: list[dict[str, Any]],
    output: Path,
    base: dict[str, Any],
) -> dict[str, Any]:
    replacements = {}
    for row in selected:
        record = json.loads((output / "results" / f"{row['run_id']}.json").read_text())
        response = record["response"]
        if record["run_id"] != row["run_id"]:
            raise ValueError("retry result run ID mismatch")
        if (
            response.get("status") != "error"
            and response.get("base_sha256") != base["sha256"]
        ):
            raise ValueError("retry used an unexpected knowledge base")
        replacements[row["run_id"]] = response
    combined = copy.deepcopy(original["per_run"])
    for row in combined:
        response = replacements.get(row["run_id"])
        if response is not None:
            row["methods"]["exploratory_rag"] = _score(
                response, row["ground_truth_service"], row["chaos_type"]
            )
            row["retry_retrieval"] = {
                key: response.get(key)
                for key in ("retrieved", "provided_pair_ids", "omitted_pair_ids")
            }
    subset = [row for row in combined if row["run_id"] in replacements]
    return {
        "version": RETRY_VERSION,
        "campaign_id": original["campaign_id"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "retry_count": len(subset),
        "retry_summary": summarize(subset),
        "combined_summary": summarize(combined),
        "completed_summary": summarize(
            [row for row in combined if row["run_status"] == "completed"]
        ),
        "retry_by_effect": {
            str(effect).lower(): summarize(
                [row for row in subset if row["effect_validated"] == effect]
            )
            for effect in (True, False)
        },
        "per_run": combined,
        "limitations": [
            "Diagnostic retries only; no fault injections, run-status changes or new load.",
            "Only failed exploratory_rag diagnostics were rerun; other answers are historical.",
            "Derived base changes the inference model identity, not training guides or index.",
            "Historical windows are queried live again; evidence can differ due to retention.",
            "Combined scores mix original and retry attempts; not a new controlled paired trial.",
            "Original retrieval statistics are not recomputed; retry retrieval is stored separately.",
        ],
    }


def run(campaign: Path, output: Path, source_base: Path | None = None) -> int:
    campaign, output = campaign.resolve(), output.resolve()
    if output == campaign or not output.is_relative_to(campaign / "diagnostic_retries"):
        raise ValueError(
            "output must be a separate directory under campaign/diagnostic_retries"
        )
    config_path = campaign / "config.yaml"
    evaluation_path = campaign / "exploratory_rag_evaluation.json"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not config["knowledge_campaign"].get("exploratory"):
        raise ValueError("only exploratory campaigns support this retry")
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    if evaluation["campaign_id"] != config["experiment_campaign_id"]:
        raise ValueError("campaign and evaluation IDs disagree")
    selected = failed_cases(campaign, evaluation)
    health = llm_health()
    if not health.get("ok") or health.get("model") != DEFAULT_LLM_MODEL:
        raise ValueError(f"model health/identity check failed: {health}")
    frozen = json.loads((campaign / "rag_base.json").read_text(encoding="utf-8"))
    source_path = source_base or (
        OBJECT_STORE
        / ".rca_knowledge"
        / config["knowledge_campaign"]["corpus_id"]
        / "exploratory_base.json"
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    retry_id = "model-retry-" + digest([source["sha256"], DEFAULT_LLM_MODEL])[:24]
    store = KnowledgeStore(output / "knowledge", retry_id)
    options = KnowledgeOptions(
        **{
            **options_for(config, "exploratory_rag"),
            "corpus_id": retry_id,
        }
    )
    inputs = []
    for row in selected:
        attempt = campaign / "attempts" / row["cell_id"]
        run_path, spec_path = attempt / "run.json", attempt / "spec.json"
        saved = json.loads(run_path.read_text(encoding="utf-8"))
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if saved["run_id"] != row["run_id"] or saved["status"] != "completed":
            raise ValueError("retry requires the original completed run")
        window = saved["timings"]["controlled_observation_window"]
        inputs.append(
            {
                "run_id": row["run_id"],
                "window": window,
                "namespace": spec["scope"]["namespace"],
                "mcp_url": spec["observability"]["metrics"]["mcp_sse_url"],
                "source_hashes": {
                    "run": file_hash(run_path),
                    "spec": file_hash(spec_path),
                    "failed_diagnostic": file_hash(
                        campaign
                        / "rag_results"
                        / row["cell_id"]
                        / "exploratory_rag.json"
                    ),
                },
            }
        )
    lock = KnowledgeStore(output.parent, output.name)
    with lock.locked():
        base = derive_base(source, frozen["sha256"], store)
        manifest = {
            "version": RETRY_VERSION,
            "campaign": str(campaign),
            "source_config_sha256": file_hash(config_path),
            "source_evaluation_sha256": file_hash(evaluation_path),
            "source_base_sha256": source["sha256"],
            "derived_base_sha256": base["sha256"],
            "source_model": source["model"],
            "retry_model": DEFAULT_LLM_MODEL,
            "llm_url": DEFAULT_LLM_URL,
            "model_health": health,
            "changed_base_fields": ["model"],
            "options": options.model_dump(),
            "inputs": inputs,
        }
        write_once(output / "manifest.json", manifest)
        if (output / "evaluation.json").exists():
            saved_report = json.loads((output / "evaluation.json").read_text())
            print(
                json.dumps(saved_report["retry_summary"], ensure_ascii=False),
                flush=True,
            )
            return 0
        for number, item in enumerate(inputs, 1):
            run_id = item["run_id"]
            path = output / "results" / f"{run_id}.json"
            if path.exists():
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous["run_id"] != run_id or previous[
                    "manifest_sha256"
                ] != digest(manifest):
                    raise ValueError("saved retry result provenance mismatch")
                if previous["response"]["status"] == "error":
                    raise RuntimeError(
                        f"preserved retry error for {run_id}; use a new attempt"
                    )
                print(f"{number}/{len(inputs)} {run_id}: preserved", flush=True)
                continue
            store.load_exploratory_base()
            current_health = llm_health()
            if (
                not current_health.get("ok")
                or current_health.get("model") != DEFAULT_LLM_MODEL
            ):
                raise ValueError(f"model health/identity changed: {current_health}")
            print(f"{number}/{len(inputs)} {run_id}: investigating", flush=True)
            window = item["window"]
            response = investigate(
                client=MCPToolClient(item["mcp_url"], 30),
                phases={
                    k: v
                    for k, v in window["phases"].items()
                    if k in {"baseline", "fault", "post"}
                },
                namespace=item["namespace"],
                window_metadata={
                    k: window[k]
                    for k in ("phase_sources", "partial", "sampling_interval_seconds")
                    if k in window
                },
                options=options,
                run_id=run_id,
                store=store,
                llm=call_llm_tool_messages,
            )
            response["model"] = DEFAULT_LLM_MODEL
            write_once(
                path,
                {
                    "run_id": run_id,
                    "method": "exploratory_rag",
                    "manifest_sha256": digest(manifest),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "response": response,
                },
            )
            print(
                f"{number}/{len(inputs)} {run_id}: {response['status']}"
                + (f" - {response['error']}" if response.get("error") else ""),
                flush=True,
            )
            if response["status"] == "error":
                raise RuntimeError(
                    f"retry failed for {run_id}; result preserved, stopping before next run"
                )
        report = evaluate_retries(evaluation, selected, output, base)
        write_once(output / "evaluation.json", report)
        print(json.dumps(report["retry_summary"], ensure_ascii=False), flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    return run(args.campaign, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
