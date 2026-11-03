import json
from collections import Counter
from unittest.mock import Mock

import pytest
import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import exploratory_rag_campaign as exploratory
from experiment.eval.memory_hog import rag_campaign as rag
from experiment.platform.backend.rca_knowledge import (
    DEFAULT_LLM_MODEL,
    VERSION,
    write_once,
)


def config(tmp_path):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    return campaign.build_config(source, tmp_path, "rag", 42, {})


def test_exact_matrix_and_split(tmp_path):
    cfg = config(tmp_path)
    plan = campaign.campaign_cells(cfg)
    assert len(plan) == 70
    assert len({cell["cell_id"] for cell in plan}) == 70
    assert [cell["phase"] for cell in plan] == ["train"] * 35 + ["test"] * 35
    for phase in ("train", "test"):
        cells = cfg[f"{phase}_experiments"]
        assert len(cells) == 35
        assert len({(c["service"], c["chaos_type"]) for c in cells}) == 35
        assert all(cell["replica_idx"] == (phase == "test") for cell in cells)
    assert set(Counter((c["service"], c["chaos_type"]) for c in plan).values()) == {2}
    assert cfg["replicas"] == 1
    assert plan == campaign.build_plan("rag", 42)


def test_unvalidated_experimental_policy_only_applies_to_exploratory_rag(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["include_unvalidated_experimental"] = True

    assert (
        rag.options_for(cfg, "exploratory_rag")["include_unvalidated_experimental"]
        is True
    )
    for mode in ("original", "exploratory_control"):
        assert rag.options_for(cfg, mode)["include_unvalidated_experimental"] is False


def test_opensearch_is_enabled_only_for_exploratory_rag(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"].update(
        retrieval_backend="opensearch",
        opensearch_base_sha256="a" * 64,
        opensearch_source_skill_sha256="b" * 64,
        opensearch_source_model="qwen-14b",
        opensearch_current_model=DEFAULT_LLM_MODEL,
        allow_legacy_model_mismatch=True,
        retriever_version="opensearch-hybrid-rrf-v1",
    )

    rag_options = rag.options_for(cfg, "exploratory_rag")
    control_options = rag.options_for(cfg, "exploratory_control")

    assert rag_options["retrieval_backend"] == "opensearch"
    assert rag_options["opensearch_base_sha256"] == "a" * 64
    assert rag_options["opensearch_source_skill_sha256"] == "b" * 64
    assert rag_options["opensearch_source_model"] == "qwen-14b"
    assert rag_options["opensearch_current_model"] == DEFAULT_LLM_MODEL
    assert rag_options["allow_legacy_model_mismatch"] is True
    assert rag_options["retriever_version"] == "opensearch-hybrid-rrf-v1"
    assert control_options["retrieval_backend"] == "local"
    assert control_options["opensearch_base_sha256"] is None
    assert control_options["opensearch_source_model"] is None
    assert control_options["allow_legacy_model_mismatch"] is False
    cfg["knowledge_campaign"].pop("retriever_version")
    assert (
        rag.options_for(cfg, "exploratory_rag")["retriever_version"]
        == "opensearch-hybrid-rrf-v2"
    )


def test_exploratory_modes_use_a_bounded_token_budget(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["max_total_tokens"] = 180_000

    assert rag.options_for(cfg, "exploratory_control")["max_total_tokens"] == 180_000
    assert rag.options_for(cfg, "exploratory_rag")["max_total_tokens"] == 180_000

    del cfg["knowledge_campaign"]["max_total_tokens"]
    assert (
        rag.options_for(cfg, "exploratory_rag")["max_total_tokens"]
        == rag.EXPLORATORY_MAX_TOTAL_TOKENS
    )


def test_history_gate_waits_for_job_and_complete_manifest(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {
        "required": True,
        "migration_timeout_seconds": 30,
    }

    class Api:
        def get(self, path):
            if path == "/api/runs/run-test":
                return {
                    "summary": {
                        "history_migration": {
                            "status": "queued",
                            "job_name": "history-migrate-test-12345678",
                            "namespace": "monitoring-history",
                        }
                    }
                }
            return {
                "status": "complete",
                "source_counts": {
                    "metrics": 1,
                    "logs": 1,
                    "traces": 1,
                    "spans": 1,
                },
            }

    monkeypatch.setattr(
        rag.subprocess,
        "run",
        lambda *args, **kwargs: campaign.subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps({"status": {"succeeded": 1}}),
            stderr="",
        ),
    )
    monkeypatch.setattr(rag.time, "sleep", lambda _: None)

    result = rag.wait_for_history_archive(cfg, "run-test", Api())

    assert result["status"] == "complete"


def test_dual_write_history_gate_waits_for_online_readback(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {
        "required": True,
        "mode": "dual_write",
        "visibility_timeout_seconds": 30,
    }

    class Api:
        calls = 0

        def get(self, path):
            assert path == "/api/runs/run-test/history/validation"
            self.calls += 1
            if self.calls == 1:
                return {"status": "pending", "telemetry": {"logs": {"status": "pending"}}}
            return {
                "status": "complete",
                "mode": "dual_write",
                "source_counts": {"metrics": 4, "logs": 2, "traces": 1},
            }

    api = Api()
    monkeypatch.setattr(rag.time, "sleep", lambda _: None)

    result = rag.wait_for_history_archive(cfg, "run-test", api)

    assert result["status"] == "complete"
    assert api.calls == 2


def test_dual_write_history_gate_stops_on_validation_failure(tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {
        "required": True,
        "mode": "dual_write",
        "visibility_timeout_seconds": 30,
    }

    class Api:
        def get(self, path):
            return {"status": "failed", "error": "missing historical tools"}

    with pytest.raises(RuntimeError, match="missing historical tools"):
        rag.wait_for_history_archive(cfg, "run-test", Api())


def test_historical_diagnostic_rejects_live_observability(tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {"required": True}

    with pytest.raises(ValueError, match="historical observability"):
        rag.validate_historical_diagnostic(
            cfg,
            "original",
            {"observability_source": "live"},
        )


def test_dual_write_diagnostic_requires_online_evidence_from_all_families(tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {"required": True, "mode": "dual_write"}
    evidence = [
        {
            "family": family,
            "source": "history",
            "status": "ok",
            "observation_count": 1,
        }
        for family in ("metrics", "logs", "traces")
    ]

    rag.validate_historical_diagnostic(
        cfg,
        "original",
        {"observability_source": "history", "evidence": evidence},
    )
    with pytest.raises(ValueError, match="lacks usable historical telemetry families"):
        rag.validate_historical_diagnostic(
            cfg,
            "exploratory_control",
            {
                "observability_source": "history",
                "evidence": evidence[:2],
            },
        )


def test_historical_endpoint_accepts_legacy_evidence_source_labels(tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {"required": True, "mode": "dual_write"}
    evidence = [
        {
            "family": family,
            "source": "live",
            "status": "ok",
            "observation_count": 1,
        }
        for family in ("metrics", "logs", "traces")
    ]

    rag.validate_historical_diagnostic(
        cfg,
        "exploratory_control",
        {
            "observability_source": "history",
            "evidence": evidence,
        },
    )


def test_historical_diagnostic_rejects_error_status_even_with_evidence(tmp_path):
    cfg = config(tmp_path)
    cfg["historical_observability"] = {"required": True, "mode": "dual_write"}
    evidence = [
        {
            "family": family,
            "source": "history",
            "status": "ok",
            "observation_count": 1,
        }
        for family in ("metrics", "logs", "traces")
    ]

    with pytest.raises(ValueError, match="diagnostic returned an error"):
        rag.validate_historical_diagnostic(
            cfg,
            "exploratory_rag",
            {
                "status": "error",
                "error": "knowledge token budget exhausted",
                "observability_source": "history",
                "evidence": evidence,
            },
        )


def test_process_cell_retries_one_persisted_diagnostic_error(
    monkeypatch, tmp_path
):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    cell = cfg["test_experiments"][0]
    directory = tmp_path / "campaign"
    directory.mkdir()
    failed_path = rag.result_path(directory, cell, "exploratory_control")
    write_once(
        failed_path,
        {
            "run_id": "run-test",
            "method": "exploratory_control",
            "response": {
                "status": "error",
                "error": "knowledge token budget exhausted",
            },
        },
    )
    monkeypatch.setattr(
        rag,
        "options_for",
        lambda *_: {
            "possible_fault_types": [],
            "require_best_guess": False,
            "max_total_tokens": 300_000,
        },
    )
    monkeypatch.setattr(rag, "validate_historical_diagnostic", lambda *_: None)
    responses = []

    def post(path, *, body, request_timeout):
        responses.append((path, body, request_timeout))
        if path.endswith("/skill-rca"):
            return {"status": "inconclusive"}
        return {"status": "confirmed", "version": VERSION}

    api = Mock()
    api.post.side_effect = post

    rag.process_cell(
        directory,
        cfg,
        cell,
        {"run_id": "run-test"},
        api,
    )

    retry_path = failed_path.with_name("exploratory_control.retry-1.json")
    assert failed_path.is_file()
    assert retry_path.is_file()
    assert json.loads(failed_path.read_text())["response"]["status"] == "error"
    retry = json.loads(retry_path.read_text())
    assert retry["response"]["status"] == "confirmed"
    assert retry["retry"]["max_total_tokens"] == 300_000
    assert rag.result_path(directory, cell, "exploratory_control") == retry_path
    assert any(body.get("max_total_tokens") == 300_000 for _, body, _ in responses)


def test_process_cell_allows_a_bounded_corrective_retry(
    monkeypatch, tmp_path
):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    cell = cfg["test_experiments"][0]
    directory = tmp_path / "campaign"
    directory.mkdir()
    retry_one = rag.result_path(directory, cell, "exploratory_control").with_name(
        "exploratory_control.retry-1.json"
    )
    write_once(
        retry_one,
        {
            "run_id": "run-test",
            "method": "exploratory_control",
            "response": {
                "status": "error",
                "error": "knowledge token budget exhausted",
            },
        },
    )
    monkeypatch.setattr(
        rag,
        "options_for",
        lambda *_: {
            "possible_fault_types": [],
            "require_best_guess": False,
            "max_total_tokens": 300_000,
        },
    )
    monkeypatch.setattr(rag, "validate_historical_diagnostic", lambda *_: None)

    def post(path, *, body, request_timeout):
        if path.endswith("/skill-rca"):
            return {"status": "inconclusive"}
        return {"status": "confirmed", "version": VERSION}

    api = Mock()
    api.post.side_effect = post

    rag.process_cell(directory, cfg, cell, {"run_id": "run-test"}, api)

    retry_two = retry_one.with_name("exploratory_control.retry-2.json")
    assert retry_two.is_file()
    assert json.loads(retry_one.read_text())["response"]["status"] == "error"
    assert json.loads(retry_two.read_text())["retry"]["attempt"] == 2
    assert rag.result_path(directory, cell, "exploratory_control") == retry_two


def test_process_cell_stops_after_maximum_diagnostic_retries(
    tmp_path,
):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    cell = cfg["test_experiments"][0]
    directory = tmp_path / "campaign"
    results = directory / "rag_results" / cell["cell_id"]
    results.mkdir(parents=True)
    for method in ("original", "exploratory_rag"):
        write_once(
            results / f"{method}.json",
            {
                "run_id": "run-test",
                "method": method,
                "response": {"status": "inconclusive"},
            },
        )
    write_once(
        results / "exploratory_control.retry-2.json",
        {
            "run_id": "run-test",
            "method": "exploratory_control",
            "response": {"status": "error", "error": "still failing"},
        },
    )

    with pytest.raises(RuntimeError, match="after 2 retries"):
        rag.process_cell(directory, cfg, cell, {"run_id": "run-test"}, Mock())

    assert not (results / "exploratory_control.retry-3.json").exists()


def test_exploratory_run_can_pause_after_first_test(monkeypatch, tmp_path):
    directory = tmp_path / "campaign"
    directory.mkdir()
    (directory / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "knowledge_campaign": {"exploratory": True},
                "test_experiments": [{"phase": "test"}],
            }
        )
    )
    calls = {}

    def execute(path, **kwargs):
        calls["path"] = path
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(exploratory.controlled_campaign, "execute", execute)

    assert exploratory.run_campaign(
        directory,
        stop_after_first_test=True,
    ) == 0
    assert calls == {
        "path": directory.resolve(),
        "resume": False,
        "stop_after_first_test": True,
    }


def test_dry_run_70_without_io(monkeypatch, capsys):
    monkeypatch.setattr(
        campaign.Path, "mkdir", lambda *a, **k: pytest.fail("unexpected write")
    )
    assert campaign.main(["--level", "rag"]) == 0
    assert "70 planned runs" in capsys.readouterr().out


def test_export_preserves_roles(tmp_path):
    cfg = config(tmp_path)
    campaign.export_runs(tmp_path, campaign.campaign_cells(cfg), [])
    import csv

    with (tmp_path / "runs.csv").open() as handle:
        counts = Counter(row["phase"] for row in csv.DictReader(handle))
    assert counts == {"train": 35, "test": 35}


def test_freeze_blocks_unfinished_train_and_uses_only_train_ids(tmp_path):
    cfg = config(tmp_path)
    api = Mock()
    with pytest.raises(ValueError, match="unfinished"):
        rag.prepare_test(tmp_path, cfg, {}, api)
    api.post.assert_not_called()
    latest = {}
    for index, cell in enumerate(cfg["train_experiments"]):
        run_id = f"train-{index}"
        latest[cell["cell_id"]] = {"status": "completed", "run_id": run_id}
        write_once(
            rag.result_path(tmp_path, cell, "annotate"),
            {
                "run_id": run_id,
                "response": {"status": "inconclusive"},
            },
        )
    api.post.return_value = {
        "version": VERSION,
        "sha256": "frozen",
        "eligible_count": 1,
    }
    rag.prepare_test(tmp_path, cfg, latest, api)
    assert len(api.post.call_args.kwargs["body"]["training_run_ids"]) == 35
    rag.prepare_test(tmp_path, cfg, latest, api)
    assert api.post.call_count == 1


def test_exploratory_manifest_counter_reconciles_unique_started_test_attempts(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    manifest_path = tmp_path / "exploratory_manifest.json"
    manifest_path.write_text(json.dumps({
        "mode": "exploratory",
        "corpus_id": cfg["knowledge_campaign"]["corpus_id"],
        "test_runs_started": 0,
    }))
    first, second = cfg["test_experiments"][:2]
    ledger = [
        {"cell_id": cfg["train_experiments"][0]["cell_id"], "run_id": "train-1"},
        {"cell_id": first["cell_id"], "attempt": 1, "run_id": "test-1", "status": "submitted"},
        {"cell_id": first["cell_id"], "attempt": 1, "run_id": "test-1", "status": "aborted"},
        {"cell_id": second["cell_id"], "attempt": 1, "status": "prepared"},
    ]

    assert rag.reconcile_exploratory_test_runs(tmp_path, cfg, ledger) == {
        "previous": 0, "reconciled": 1,
    }
    assert json.loads(manifest_path.read_text())["test_runs_started"] == 1
    assert rag.reconcile_exploratory_test_runs(tmp_path, cfg, ledger) is None


def test_exploratory_manifest_rejects_counter_ahead_of_ledger(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    manifest_path = tmp_path / "exploratory_manifest.json"
    manifest_path.write_text(json.dumps({
        "mode": "exploratory",
        "corpus_id": cfg["knowledge_campaign"]["corpus_id"],
        "test_runs_started": 2,
    }))

    with pytest.raises(ValueError, match="absent from the ledger"):
        rag.reconcile_exploratory_test_runs(tmp_path, cfg, [])


def test_test_requests_contain_no_current_ground_truth_and_are_resumable(tmp_path):
    cfg = config(tmp_path)
    cell = cfg["test_experiments"][0]
    entry = {"run_id": "opaque-test", "status": "completed"}
    api = Mock()
    api.post.return_value = {
        "version": VERSION,
        "status": "inconclusive",
        "run_id": entry["run_id"],
    }
    rag.process_cell(tmp_path, cfg, cell, entry, api)
    assert api.post.call_count == 3
    for call in api.post.call_args_list:
        body = call.kwargs["body"]
        assert "service" not in body and "labels" not in body
        assert "cell_id" not in body and "chaos_type" not in body
    rag.process_cell(tmp_path, cfg, cell, entry, api)
    assert api.post.call_count == 3


def test_exploratory_diagnostics_require_best_guess_for_every_method(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    cell = cfg["test_experiments"][0]
    api = Mock()
    api.post.return_value = {"version": VERSION, "status": "inconclusive"}

    rag.process_cell(
        tmp_path, cfg, cell, {"run_id": "opaque-test", "status": "completed"}, api
    )

    assert api.post.call_count == 3
    assert all(call.kwargs["body"]["require_best_guess"] is True for call in api.post.call_args_list)
    original_call = next(
        call for call in api.post.call_args_list
        if call.args[0].endswith("/skill-rca")
    )
    assert original_call.kwargs["body"]["possible_fault_types"]


def test_exploratory_evaluation_aggregates_all_35_tests(tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    cells = cfg["test_experiments"]
    first = cells[0]
    campaign.append_ledger(
        tmp_path / "ledger.jsonl",
        {
            "cell_id": first["cell_id"],
            "run_id": "test-run",
            "status": "completed",
            "gates": {
                "intervention_confirmed_on_target": True,
                "mechanism_observed": {"passed": True},
            },
        },
    )
    write_once(tmp_path / "rag_base.json", {"sha256": "base"})
    write_once(
        tmp_path / "label_audit.json",
        {
            "sha256": "audit",
            "counts": {
                "b_records_audited": 32,
                "label_matches": 32,
                "label_mismatches": 0,
                "effect_validated_pairs": 24,
                "effect_unvalidated_pairs": 8,
            },
            "records": [
                {
                    "run_id": "source-train-run",
                    "pair_id": "source-pair",
                    "target_labels": {
                        "service": first["service"],
                        "fault_type": first["chaos_type"],
                        "causal_category": "network-latency",
                    },
                    "labels_match": True,
                }
            ],
        },
    )
    for method in rag.EXPLORATORY_METHODS:
        response = {
            "status": "inconclusive",
            "diagnosis": {
                "status": "inconclusive",
                "primary_service": first["service"],
                "fault_type": first["chaos_type"],
                "supporting_evidence": [],
            },
            "request_duration_ms": 10,
        }
        if method == "exploratory_control":
            response["base_sha256"] = None
        if method == "exploratory_rag":
            response.update(
                base_sha256="base",
                retrieved=[
                    {
                        "pair_id": "source-pair",
                        "source_run_id": "source-train-run",
                        "score": 0.9,
                    }
                ],
                retrieval={"top_k": 3},
            )
        write_once(
            rag.result_path(tmp_path, first, method),
            {"run_id": "test-run", "response": response},
        )

    report = rag.build_exploratory_evaluation(tmp_path, cfg)

    assert report["planned"] == {"test": 35}
    assert report["outcomes"] == {"completed": 1, "not_run": 34}
    assert len(report["per_run"]) == 35
    assert report["summary"]["exploratory_rag"]["denominator"] == 35
    assert report["summary"]["exploratory_rag"]["classification_complete"] == 1
    assert report["summary"]["exploratory_rag"]["joint_correct"] == 1
    assert report["per_run"][0]["retrieval"]["same_combination_recalled"] is True
    assert report["recall_at_k"] == pytest.approx(1 / 35)
    assert report["ground_truth_accessed_after_diagnostics_persisted"] is True


def test_exploratory_runner_can_resume_prepared_campaign(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    calls = []
    monkeypatch.setattr(
        exploratory.controlled_campaign,
        "execute",
        lambda directory, *, resume, stop_after_first_test=False: calls.append(
            (directory, resume, stop_after_first_test)
        )
        or 7,
    )

    assert exploratory.run_campaign(tmp_path, resume=True) == 7
    assert calls == [(tmp_path.resolve(), True, False)]


def test_exploratory_runner_can_pause_after_first_test_while_resuming(
    monkeypatch, tmp_path
):
    cfg = config(tmp_path)
    cfg["knowledge_campaign"]["exploratory"] = True
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    calls = []
    monkeypatch.setattr(
        exploratory.controlled_campaign,
        "execute",
        lambda directory, *, resume, stop_after_first_test=False: calls.append(
            (directory, resume, stop_after_first_test)
        )
        or 7,
    )

    assert exploratory.run_campaign(
        tmp_path,
        resume=True,
        stop_after_first_test=True,
    ) == 7
    assert calls == [(tmp_path.resolve(), True, True)]


def test_annotation_error_stops_before_next_injection(tmp_path):
    cfg = config(tmp_path)
    api = Mock()
    api.post.return_value = {
        "version": VERSION,
        "status": "error",
        "error": "No telemetry",
    }
    with pytest.raises(RuntimeError, match="no new injection"):
        rag.process_cell(
            tmp_path, cfg, cfg["train_experiments"][0], {"run_id": "train"}, api
        )
    assert not rag.result_path(
        tmp_path, cfg["train_experiments"][0], "annotate"
    ).exists()
    assert list((tmp_path / "rag_results").glob("*/annotation_error_*.json"))


def test_planned_denominator_includes_missing_diagnoses(tmp_path):
    cfg = config(tmp_path)
    cell = cfg["test_experiments"][0]
    campaign.append_ledger(
        tmp_path / "ledger.jsonl",
        {
            "cell_id": cell["cell_id"],
            "run_id": "test",
            "status": "completed",
            "gates": {
                "intervention_confirmed_on_target": True,
                "mechanism_observed": {"passed": True},
            },
        },
    )
    write_once(tmp_path / "rag_base.json", {"sha256": "base"})
    write_once(
        rag.result_path(tmp_path, cell, "rag"),
        {
            "run_id": "test",
            "response": {
                "status": "inconclusive",
                "base_sha256": "base",
                "diagnosis": {
                    "primary_service": cell["service"],
                    "fault_type": cell["chaos_type"],
                },
            },
        },
    )
    report = rag.write_evaluation(tmp_path, cfg)
    assert report["planned"] == {"total": 70, "train": 35, "test": 35}
    assert len(report["per_run"]) == 35
    assert report["summary"]["rag"]["joint_correct"] == 1
    assert report["summary"]["rag"]["denominator"] == 35
    assert report["summary"]["rag"]["joint_accuracy"] == 1 / 35
    assert report["completed_summary"]["rag"]["denominator"] == 1
    assert report["paired"]["rag_vs_control"] == {
        "denominator": 35,
        "gains": 1,
        "losses": 0,
        "net": 1,
    }
    assert report["outcomes"]["train"] == {"not_run": 35}
    assert (
        json.loads((tmp_path / "rag_evaluation.json").read_text())["summary"]
        == report["summary"]
    )


def test_evaluator_rejects_mixed_bases(tmp_path):
    cfg = config(tmp_path)
    cell = cfg["test_experiments"][0]
    campaign.append_ledger(
        tmp_path / "ledger.jsonl",
        {
            "cell_id": cell["cell_id"],
            "run_id": "test",
            "status": "completed",
        },
    )
    write_once(tmp_path / "rag_base.json", {"sha256": "expected"})
    write_once(
        rag.result_path(tmp_path, cell, "rag"),
        {
            "run_id": "test",
            "response": {"status": "inconclusive", "base_sha256": "wrong"},
        },
    )
    with pytest.raises(ValueError, match="different knowledge base"):
        rag.build_evaluation(tmp_path, cfg)


def test_full_70_run_orchestration_and_resume_without_cluster(monkeypatch, tmp_path):
    cfg = config(tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "plan.json").write_text(json.dumps(campaign.campaign_cells(cfg)))
    starts = []
    annotations = set()
    diagnostics = []
    frozen = []

    class API:
        base_url = cfg["platform"]["api_base"]

        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            if path == "/api/skill-rag/strategy":
                return {
                    "version": VERSION,
                    "modes": [
                        "annotate", "control", "rag",
                        "exploratory_control", "exploratory_rag",
                    ],
                }
            if path == "/api/runs":
                return []
            assert path.startswith("/api/runs/")
            return {
                "status": "completed",
                "summary": {
                    "controlled_status": "completed",
                    "controlled_gates": {
                        "submitted": True,
                        "intervention_confirmed_on_target": True,
                        "mechanism_observed": {"passed": True},
                        "recovery": {"passed": True},
                        "cleanup": {"passed": True},
                        "load_stopped": {"stopped": True},
                    },
                },
            }

        def post(self, path, body=None, **kwargs):
            if path == "/api/experiments":
                assert body["spec"]["knowledge_campaign"]["split"] in {"train", "test"}
                return {"version": 1}
            if path == "/api/runs/start":
                starts.append(body)
                if not body["is_training"]:
                    assert frozen
                return {"run_id": f"run-{len(starts)}"}
            if path.endswith("/freeze"):
                assert len(starts) == 35 and len(annotations) == 35
                assert set(body["training_run_ids"]) == annotations
                frozen.append(True)
                return {"version": VERSION, "sha256": "base", "eligible_count": 35}
            run_id = path.split("/")[3]
            if body.get("mode") == "annotate":
                assert len(starts) <= 35
                annotations.add(run_id)
            else:
                assert frozen
                diagnostics.append((run_id, body.get("mode", "original")))
            return {
                "version": VERSION,
                "status": "inconclusive",
                "diagnosis": {},
                "base_sha256": "base",
            }

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "require_environment", lambda *args: None)
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *args: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *args: None)
    monkeypatch.setattr(
        campaign,
        "evaluate_campaign",
        lambda *args, **kwargs: pytest.fail("legacy evaluator"),
    )
    assert campaign.execute(tmp_path, resume=False) == 0
    assert len(starts) == 70
    assert [entry["is_training"] for entry in starts] == [True] * 35 + [False] * 35
    assert len(diagnostics) == 105
    assert len(set(diagnostics)) == 105
    assert len(annotations) == 35
    assert len(frozen) == 1
    assert campaign.execute(tmp_path, resume=True) == 0
    assert len(starts) == 70
    assert len(diagnostics) == 105
    report = json.loads((tmp_path / "rag_evaluation.json").read_text())
    assert report["outcomes"] == {"train": {"completed": 35}, "test": {"completed": 35}}
    assert report["execution_attempts"]["planned_cells"] == 70
    assert report["execution_attempts"]["submitted_attempts"] == 70
    assert report["execution_attempts"]["additional_attempts"] == 0


def test_reviewed_rag_training_retry_preserves_split_and_excludes_aborted_run(
    monkeypatch, tmp_path,
):
    cfg = config(tmp_path)
    cells = campaign.campaign_cells(cfg)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "plan.json").write_text(json.dumps(cells))
    retry_cell = next(
        cell for cell in cfg["train_experiments"]
        if cell["chaos_type"] == "container-kill"
    )
    original_run_id = "aborted-training-run"
    retry_dir = campaign.attempt_directory(tmp_path, retry_cell["cell_id"], 1)
    retry_dir.mkdir(parents=True)
    (retry_dir / "run.json").write_text(json.dumps({
        "status": "failed",
        "summary": {
            "controlled_status": "aborted",
            "controlled_gates": {
                "submitted": True,
                "mechanism_observed": {"passed": True, "restart_count_delta": 1},
                "recovery": {"passed": False},
                "cleanup": {"passed": True},
                "load_stopped": {"stopped": True},
                "application_stability": {"healthy": True},
            },
        },
    }))
    campaign.append_ledger(tmp_path / "ledger.jsonl", {
        "cell_id": retry_cell["cell_id"], "phase": "train", "status": "aborted",
        "attempt": 1, "run_id": original_run_id,
    })

    training_ids = set()
    for cell in cfg["train_experiments"]:
        if cell["cell_id"] == retry_cell["cell_id"]:
            continue
        run_id = f"completed-{cell['cell_id']}"
        campaign.append_ledger(tmp_path / "ledger.jsonl", {
            "cell_id": cell["cell_id"], "phase": "train", "status": "completed",
            "attempt": 1, "run_id": run_id,
        })
        training_ids.add(run_id)
        write_once(rag.result_path(tmp_path, cell, "annotate"), {
            "run_id": run_id, "response": {"status": "inconclusive"},
        })

    starts = []
    freeze_requests = []

    class API:
        base_url = cfg["platform"]["api_base"]

        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            if path == "/api/skill-rag/strategy":
                return {
                    "version": VERSION,
                    "modes": [
                        "annotate", "control", "rag",
                        "exploratory_control", "exploratory_rag",
                    ],
                }
            if path == "/api/runs":
                return []
            assert path.startswith("/api/runs/")
            return {
                "status": "completed",
                "summary": {
                    "controlled_status": "completed",
                    "controlled_gates": {
                        "submitted": True,
                        "intervention_confirmed_on_target": True,
                        "mechanism_observed": {"passed": True},
                        "recovery": {"passed": True},
                        "cleanup": {"passed": True},
                        "load_stopped": {"stopped": True},
                    },
                },
            }

        def post(self, path, body=None, **kwargs):
            if path == "/api/experiments":
                spec = body["spec"]
                assert spec["knowledge_campaign"]["split"] in {"train", "test"}
                if retry_cell["cell_id"] in spec["experiment"]["id"]:
                    assert spec["experiment"]["id"].endswith("-attempt-2")
                return {"version": 1}
            if path == "/api/runs/start":
                starts.append(body)
                return {"run_id": f"replacement-{len(starts)}"}
            if path.endswith("/freeze"):
                freeze_requests.append(body["training_run_ids"])
                assert len(body["training_run_ids"]) == 35
                assert original_run_id not in body["training_run_ids"]
                assert "replacement-1" in body["training_run_ids"]
                return {"version": VERSION, "sha256": "frozen-base", "eligible_count": 35}
            return {
                "version": VERSION,
                "status": "inconclusive",
                "diagnosis": {},
                "base_sha256": "frozen-base",
                "retrieved": [],
            }

    monkeypatch.setattr(campaign.runner, "ApiClient", API)
    monkeypatch.setattr(campaign, "require_environment", lambda *args: None)
    monkeypatch.setattr(campaign, "require_controlled_backend", lambda *args: None)
    monkeypatch.setattr(campaign, "require_scrape_metadata_tool", lambda *args: None)
    assert campaign.execute(
        tmp_path, resume=True,
        retry_reviewed_container_kill={retry_cell["cell_id"]},
    ) == 0

    assert len(starts) == 36
    assert starts[0]["is_training"] is True
    assert len(freeze_requests) == 1
    assert len(training_ids) == 34
    protocol = json.loads((tmp_path / campaign.RAG_REVIEWED_RETRY_PROTOCOL_FILE).read_text())
    assert protocol["previous_run_id"] == original_run_id
    assert protocol["additional_attempt_budget"] == 1
    assert protocol["split_unchanged"] == {"train": 35, "test": 35}
    report = json.loads((tmp_path / "rag_evaluation.json").read_text())
    assert report["planned"] == {"total": 70, "train": 35, "test": 35}
    assert report["execution_attempts"]["submitted_attempts"] == 71
    assert report["execution_attempts"]["additional_attempts"] == 1
    assert report["outcomes"] == {
        "train": {"completed": 35},
        "test": {"completed": 35},
    }
    assert campaign.execute(tmp_path, resume=True) == 0
    assert len(starts) == 36

    starts_before_duplicate = len(starts)
    with pytest.raises(RuntimeError, match="first-attempt training cell"):
        campaign.execute(
            tmp_path, resume=True,
            retry_reviewed_container_kill={retry_cell["cell_id"]},
        )
    assert len(starts) == starts_before_duplicate
