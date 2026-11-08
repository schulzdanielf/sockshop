from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from experiment.eval.memory_hog import training_b_rebuild as rebuild
from experiment.eval.memory_hog.training_b_queries import (
    HistoricalQueries,
    evidence_context,
    summarize_result,
)


def test_audit_decision_rejects_approval_with_issues_and_nonquery_checks():
    with pytest.raises(ValidationError):
        rebuild.AuditDecision(
            action="approve", issues=["unsupported CPU claim"], checks=[],
            rationale="Ainda ha uma contradicao.",
        )
    with pytest.raises(ValidationError):
        rebuild.AuditDecision(action="query", issues=[], checks=[], rationale="Consultar.")
    check = {
        "kind": "memory", "service": "user", "phase": "fault",
        "hypothesis": "Distinguir pressao de memoria",
        "expected_support": "Memoria aumentou",
        "expected_refutation": "Memoria sem alteracao",
        "historical_pair_ids": [], "patterns": [],
    }
    correction = rebuild.AuditDecision(
        action="correct", issues=["Falta evidencia"], checks=[check],
        rationale="A consulta pode obter evidencia para a correcao.",
    )
    assert correction.action == "correct" and len(correction.checks) == 1


def test_audit_budget_checks_before_call():
    budget = rebuild.AuditBudget()
    for _ in range(21):
        budget.reserve([], 1)
    with pytest.raises(ValueError, match="audit budget exhausted"):
        budget.reserve([], 1)
    budget = rebuild.AuditBudget()
    with pytest.raises(ValueError, match="audit budget exhausted"):
        budget.reserve([{"content": "x" * 2400000}], 1)
    assert budget.calls == 0
    assert budget.snapshot()["max_calls"] == 21
    assert budget.snapshot()["max_reserved_tokens"] == 450_000


def test_freezing_renewed_a_recomputes_vectors(monkeypatch):
    from experiment.platform.tests.test_rca_knowledge import document
    provider = rebuild.get_embedding_provider()
    pair = {"pair_id": "pair-1", "source_run_id": "run-train", "a": document()}
    monkeypatch.setattr(rebuild, "_updated_pair", lambda original, result: pair)
    source = {"pairs": [pair], "index": {"pair-1": [0.0] * provider.dim}}
    results = {"run-train": {"audit_provenance": {"base_sha256": "frozen"}}}
    base = rebuild._freeze_base(source, results, {
        "historical_evidence_manifest_sha256": "manifest",
        "historical_evidence_results_sha256": "results",
    })
    expected = provider.embed(rebuild.IncidentA.model_validate(pair["a"]).retrieval_text())
    assert base["index"]["pair-1"] == expected.tolist()
    assert base["index"]["pair-1"] != source["index"]["pair-1"]


def test_training_evidence_projection_preserves_facts_under_tighter_budget():
    from experiment.eval.memory_hog.training_b_queries import evidence_context
    from experiment.platform.tests.test_rca_knowledge import metric_evidence
    projected = evidence_context(metric_evidence())
    assert len(rebuild.canonical(projected)) <= 72_000
    assert "54.73" in rebuild.canonical(projected)


def test_training_evidence_projection_keeps_large_critical_paths():
    rows = [
        {"service": f"service-{index}", "fact": "x" * 80}
        for index in range(600)
    ]
    projected = evidence_context([{
        "id": "N001", "tool": "get_run_golden_signals",
        "family": "metrics", "source": "history", "status": "ok",
        "arguments": {}, "digest": {"critical_path": rows},
    }])
    encoded = rebuild.canonical(projected)
    critical_path = projected[0]["digest_summary"]["critical_path"]

    assert 40_000 < len(encoded) <= 72_000
    assert [
        dict(zip(critical_path["columns"], row))
        for row in critical_path["rows"]
    ] == rows


def test_target_metric_context_links_to_current_evidence_without_copying_results():
    original = [{
        "evidence_id": "N016", "kind": "http", "service": "shipping",
        "phase": "fault", "tool": "prometheus_range_query", "status": "ok",
        "usable": True, "result": {"values": ["observed"] * 500},
    }]
    compact = rebuild._metric_evidence_context(original)
    assert "result" not in compact[0]
    assert compact[0]["result_location"] == "source_evidence[id=N016].digest_summary"
    assert len(original[0]["result"]["values"]) == 500


def test_renewed_pair_a_references_its_new_online_evidence():
    from experiment.platform.tests.test_rca_knowledge import document, metric_evidence
    from experiment.platform.backend.rca_knowledge import validate_a
    evidence = metric_evidence()
    evidence[0]["id"] = "N010"
    factual = rebuild._live_training_a(SimpleNamespace(evidence=evidence))
    source = {"a": document(), "evidence": metric_evidence()}
    result = {
        "factual_a_enabled": True, "observed_incident_a": factual.model_dump(),
        "guide": {"diagnosis": {"status": "inconclusive"}},
        "status": "procedural_pending", "procedural_review": {},
        "supervised_investigation": {}, "evidence_index": [],
        "collection_evidence": evidence, "production_status": "insufficient_evidence",
        "retrieval_history": {"historical_status": "procedural_pending"},
        "gate_reasons": ["missing_metric"],
    }
    pair = rebuild._updated_pair(source, result)
    validate_a(rebuild.IncidentA.model_validate(pair["a"]), pair["evidence"], rebuild.TARGET_SERVICES)
    assert source["evidence"][0]["id"] == "E001"


def test_causal_gate_does_not_count_span_metrics_as_independent_of_traces():
    evidence = {
        "N001": _evidence("get_run_golden_signals", {}),
        "N002": _evidence("tempo_service_graph_phases", {}, "traces"),
    }
    diagnosis = {"supporting_evidence": list(evidence), "causal_chain": ["Sintoma observado."]}
    assert "causal_independent_families_missing" in rebuild._causal_issues(
        diagnosis, evidence, "user"
    )
    evidence["N003"] = _evidence("get_memory_saturation_top", {})
    diagnosis["supporting_evidence"].append("N003")
    assert rebuild._causal_issues(diagnosis, evidence, "user") == []


def test_online_training_a_ignores_old_narrative():
    from experiment.platform.tests.test_rca_knowledge import metric_evidence
    session = SimpleNamespace(evidence=metric_evidence())
    a = rebuild._live_training_a(session)
    assert a.metric_narrative_version == "facts-v1"
    assert a.metric_facts[0].service == "catalogue"
    assert "Servico=catalogue" in a.metric_changes[0].description
    with pytest.raises(ValueError, match="usable online golden"):
        rebuild._live_training_a(SimpleNamespace(evidence=[]))


def test_rag_retrieval_excludes_self_duplicates_and_pending(monkeypatch):
    from experiment.platform.tests.test_rca_knowledge import document
    incident = rebuild.IncidentA.model_validate(document())
    duplicate = {**document(), "limitations": ["Duplicate with different metadata"]}
    unique = {**document(), "symptoms": [{
        "description": "Outro sintoma observado", "evidence_ids": ["E001"],
    }]}
    rag = rebuild.TrainingAuditRAG.__new__(rebuild.TrainingAuditRAG)
    rag.provenance = {"base_sha256": "frozen"}
    rag.base = {"pairs": [
        {"pair_id": "self", "source_run_id": "run-self", "a": document()},
        {"pair_id": "duplicate", "source_run_id": "run-other", "a": duplicate,
         "guide_status": "procedurally_validated", "b_procedural_status": "passed"},
        {"pair_id": "unique", "source_run_id": "run-third", "a": unique,
         "guide_status": "procedurally_validated", "b_procedural_status": "passed"},
    ]}
    rag.index = SimpleNamespace(search=lambda *args, **kwargs: [
        {"pair_id": "duplicate"}, {"pair_id": "unique"},
    ])
    monkeypatch.setattr(rebuild, "compact_exploratory_history", lambda pair, **kw: {
        "pair_id": pair["pair_id"], "notice": "Historico nao e evidencia atual",
    })
    result = rag.retrieve(incident, "run-self")
    assert result["provided_pair_ids"] == ["unique"]
    assert result["omitted"] == [{"pair_id": "duplicate", "reason": "duplicate_incident"}]
    rag.base["pairs"][2]["b_procedural_status"] = "pending"
    with pytest.raises(ValueError, match="ineligible or self"):
        rag.retrieve(incident, "run-self")


def test_audit_rag_refuses_test_runs_before_connecting(monkeypatch, tmp_path):
    base = {
        "b_rebuild_version": rebuild.TRAINING_B_SCHEMA,
        "pairs": [{"split": "test", "source_run_id": "run-test"}],
    }
    monkeypatch.setattr(rebuild, "KnowledgeStore", lambda *args: SimpleNamespace(
        load_exploratory_base=lambda **kwargs: base,
    ))
    with pytest.raises(ValueError, match="only procedural guides from training"):
        rebuild.TrainingAuditRAG(tmp_path / "corpus/exploratory_base.json", {"run-train"})


def test_coverage_collects_all_families_and_windows_and_persists_no_data(tmp_path):
    session = HistoricalQueries.__new__(HistoricalQueries)
    session.windows = {
        phase: {"start": f"{phase}-start", "end": f"{phase}-end"}
        for phase in ("baseline", "fault", "post")
    }
    session.directory = tmp_path
    calls = []
    def query(tool, arguments, family, **kwargs):
        calls.append((tool, arguments, family, kwargs["phase"]))
        return {
            "id": f"N{len(calls):03d}", "tool": tool, "family": family,
            "phase": kwargs["phase"], "arguments": arguments,
            "status": "no_data" if family == "traces" else "ok",
            "observation_count": 0 if family == "traces" else 2,
            "result_ref": "query.json", "result_sha256": "hash",
        }
    session.query = query
    coverage = session.collect_coverage()
    assert coverage["query_count"] == 9
    assert coverage["no_data_count"] == 3
    assert len({(family, phase) for _, _, family, phase in calls}) == 9
    assert all("service_name" not in args for _, args, _, _ in calls)
    assert json.loads((tmp_path / "coverage.json").read_text()) == coverage


def test_audit_loop_executes_correction_check_then_repairs_without_private_target(monkeypatch, tmp_path):
    from experiment.platform.tests.test_rca_knowledge import document
    pair = {"source_run_id": "run-train", "labels": {
        "service": "user", "fault_type": "memory-hog", "causal_category": "memory-exhaustion",
    }}
    evidence = []
    executions = []
    session = SimpleNamespace(
        evidence=evidence, calls=[], tools={}, windows={},
        index=lambda: {},
        execute=lambda check, scope: executions.append(check.kind),
    )
    guide = {"diagnosis": {}, "limitations": [], "investigation_steps": []}
    review = {}
    contexts = []
    check = {
        "kind": "memory", "service": "user", "phase": "fault",
        "hypothesis": "Distinguir pressao de memoria", "expected_support": "Memoria aumentou",
        "expected_refutation": "Memoria sem alteracao", "historical_pair_ids": ["guide-1"],
        "patterns": [],
    }
    decisions = iter([
        {"action": "correct", "issues": ["Falta memoria"], "checks": [check], "rationale": "Consultar antes de corrigir"},
        {"action": "approve", "issues": [], "checks": [], "rationale": "Procedimento corrigido"},
    ])
    gates = iter([["missing_metric"], []])
    monkeypatch.setattr(rebuild, "_gate_reasons", lambda *a, **kw: next(gates))
    monkeypatch.setattr(rebuild, "_guide_issues", lambda *a, **kw: [])
    monkeypatch.setattr(rebuild, "_target_metric_evidence", lambda *a: [])
    monkeypatch.setattr(rebuild, "check_inventory", lambda *a: {"executable_checks": []})
    monkeypatch.setattr(rebuild, "prepare_check", lambda *a: None)
    def generate(stage, schema, context, **kwargs):
        contexts.append((stage, context))
        if stage == "audit_training_b":
            result = next(decisions)
            assert kwargs["validate"](result) == []
        elif stage == "repair_audited_training_b":
            result = {**guide, "diagnosis": {"status": "inconclusive"}}
        else:
            result = {}
        return result, [], {}
    monkeypatch.setattr(rebuild, "_model_json", generate)
    rag = SimpleNamespace(retrieve=lambda *a: {
        "history": [], "provided_pair_ids": ["guide-1"],
    })
    _, _, records, revisions = rebuild._audit_and_correct(
        pair, guide, review, session, {"observed_incident_a": document()}, {},
        rag, tmp_path,
    )
    assert executions == ["memory"]
    assert len(records) == 2 and len(revisions) == 1
    for stage, context in contexts:
        if stage == "audit_training_b":
            assert "private_training_target_for_validation_only" not in context
            assert "training_annotation_labels" not in context
    assert (tmp_path / "audit_budget.json").exists()


def test_audit_stops_after_two_corrections_without_promoting_unresolved_guide(monkeypatch, tmp_path):
    from experiment.platform.tests.test_rca_knowledge import document
    pair = {"source_run_id": "run-train", "labels": {
        "service": "user", "fault_type": "memory-hog", "causal_category": "memory-exhaustion",
    }}
    session = SimpleNamespace(
        evidence=[], calls=[], tools={}, windows={}, index=lambda: {},
    )
    guide = {"diagnosis": {}, "limitations": [], "investigation_steps": []}
    monkeypatch.setattr(rebuild, "_gate_reasons", lambda *a, **kw: ["missing_metric"])
    monkeypatch.setattr(rebuild, "_guide_issues", lambda *a, **kw: [])
    monkeypatch.setattr(rebuild, "_target_metric_evidence", lambda *a: [])
    monkeypatch.setattr(rebuild, "check_inventory", lambda *a: {"executable_checks": []})
    stages = []
    def generate(stage, schema, context, **kwargs):
        stages.append(stage)
        if stage == "audit_training_b":
            value = {
                "action": "correct", "issues": ["Metrica ausente"], "checks": [],
                "rationale": "Nao ha prova suficiente",
            }
        elif stage == "repair_audited_training_b":
            value = guide
        else:
            value = {}
        return value, [], {}
    monkeypatch.setattr(rebuild, "_model_json", generate)
    rag = SimpleNamespace(retrieve=lambda *a: {"history": [], "provided_pair_ids": []})
    _, _, records, revisions = rebuild._audit_and_correct(
        pair, guide, {}, session, {"observed_incident_a": document()}, {},
        rag, tmp_path,
    )
    assert len(records) == 3 and len(revisions) == 2
    assert stages.count("review_audited_training_b") == 2
    assert records[-1]["decision"]["action"] != "approve"
    assert records[-1]["gate_reasons"] == ["missing_metric"]


@pytest.mark.parametrize(("fail_stage", "failure_type"), [
    ("audit_training_b", rebuild.ModelContractError),
    ("audit_training_b", rebuild.AuditBudgetError),
    ("repair_audited_training_b", rebuild.ModelContractError),
])
def test_audit_contract_or_budget_errors_are_persisted_as_pending(
    monkeypatch, tmp_path, fail_stage, failure_type,
):
    from experiment.platform.tests.test_rca_knowledge import document
    pair = {"source_run_id": "run-train", "labels": {
        "service": "user", "fault_type": "memory-hog", "causal_category": "memory-exhaustion",
    }}
    session = SimpleNamespace(evidence=[], calls=[], tools={}, windows={}, index=lambda: {})
    guide = {"diagnosis": {}, "limitations": [], "investigation_steps": []}
    monkeypatch.setattr(rebuild, "_gate_reasons", lambda *a, **kw: ["missing_metric"])
    monkeypatch.setattr(rebuild, "_guide_issues", lambda *a, **kw: [])
    monkeypatch.setattr(rebuild, "_target_metric_evidence", lambda *a: [])
    monkeypatch.setattr(rebuild, "check_inventory", lambda *a: {"executable_checks": []})
    def generate(stage, schema, context, **kwargs):
        if stage == fail_stage:
            if failure_type is rebuild.ModelContractError:
                raise rebuild.ModelContractError(stage, [{"attempt": 1}], "invalid response")
            raise rebuild.AuditBudgetError("budget exhausted")
        return {
            "action": "correct", "issues": ["metric missing"], "checks": [],
            "rationale": "Needs correction",
        }, [], {}
    monkeypatch.setattr(rebuild, "_model_json", generate)
    rag = SimpleNamespace(retrieve=lambda *a: {"history": [], "provided_pair_ids": []})
    _, _, records, _ = rebuild._audit_and_correct(
        pair, guide, {}, session, {"observed_incident_a": document()}, {},
        rag, tmp_path,
    )
    pending = records[-1]
    assert pending["decision"]["action"] == "pending"
    assert pending["contract_failure"]["stage"] == fail_stage
    assert pending["contract_failure"]["attempts"] == (
        [{"attempt": 1}] if failure_type is rebuild.ModelContractError else []
    )
    assert json.loads((tmp_path / "audit_budget.json").read_text())["calls"] == 0


@pytest.mark.parametrize("options", [
    {"pending_only": True},
    {"pending_only": True, "audit_rag_base": Path("reference.json"), "index": True},
    {"pending_only": True, "audit_rag_base": Path("reference.json"), "pilot_five": True},
    {"factual_a_only": True},
    {"audit_rag_base": Path("reference.json")},
    {"source_run_ids": {"run-train"}},
])
def test_invalid_selection_and_ablation_options_fail_before_collection(options):
    with pytest.raises(ValueError):
        rebuild.rebuild(**options)


def _evidence(tool: str, result: dict, family: str = "metrics") -> dict:
    return {
        "family": family,
        "tool": tool,
        "status": "ok",
        "usable": True,
        "text": rebuild.canonical(result),
    }


def test_container_kill_metric_requires_positive_target_restart_evidence():
    guide = {
        "diagnosis": {"supporting_evidence": ["E1"]},
        "investigation_steps": [
            {
                "step_id": "S1",
                "kind": "observed",
                "phase": "fault",
                "service": "user",
                "family": "lifecycle",
                "evidence_ids": ["E1"],
            }
        ],
    }
    no_restart = {"E1": _evidence("check_pod_restarts", {
        "service": "user",
        "restarts_in_window": 0,
    })}
    positive_restart = {"E1": _evidence("check_pod_restarts", {
        "affected": [{
            "service": "user", "restarts_in_window": 1,
            "pod_observations": [{
                "container": "user",
                "restart_observations": [{"restart_count_delta": 1}],
            }],
        }],
    })}

    assert not rebuild._type_specific_evidence(
        "container-kill", "user", guide, no_restart
    )
    assert rebuild._type_specific_evidence(
        "container-kill", "user", guide, positive_restart
    )

    assert not rebuild._type_specific_evidence(
        "container-kill", "user", guide, no_restart
    )


def test_target_metric_projection_selects_service_restart_with_uncertain_timing():
    calls = [{
        "tool": "check_pod_restarts",
        "kind": "lifecycle",
        "service": None,
        "phase": "fault",
        "evidence_id": "N002",
        "result": {
            "status": "ok",
            "top": [{
                "service": "user",
                "restarts_in_window": 1,
                "pods": [{
                    "container": "user",
                    "restart_observations": [{
                        "restart_count_delta": 1,
                        "counter_reset": False,
                        "window_attribution": "overlap_only",
                    }],
                }],
            }],
        },
    }]
    evidence_index = {"N002": {"status": "ok", "usable": True}}
    target = {
        "service": "user",
        "fault_type": "container-kill",
        "causal_category": "pod-failure",
    }

    projected = rebuild._target_metric_evidence(calls, evidence_index, target)

    assert projected == [{
        "evidence_id": "N002",
        "kind": "lifecycle",
        "service": "user",
        "phase": "fault",
        "tool": "check_pod_restarts",
        "status": "ok",
        "usable": True,
        "result": calls[0]["result"]["top"][0],
    }]
    assert rebuild._target_metric_query_exists(calls, "lifecycle", "user", "fault")


def test_all_service_restart_query_counts_as_attempted_without_positive_restart():
    calls = [{
        "tool": "check_pod_restarts",
        "kind": "lifecycle",
        "service": None,
        "phase": "baseline",
        "result": {
            "status": "ok",
            "top": [{"service": "user", "restarts_in_window": 0}],
        },
    }]

    assert rebuild._target_metric_query_exists(calls, "lifecycle", "user", "baseline")


def test_target_http_metric_evidence_uses_returned_phase_summary():
    fault_result = {
        "status": "ok",
        "series": [{
            "labels": {"response_code": "503"},
            "mean": 0.0,
        }],
    }
    calls = [{
        "tool": "prometheus_range_query",
        "kind": "http",
        "service": "front-end",
        "phase": "baseline",
        "evidence_id": "N009",
        "result": {
            "status": "ok",
            "phases": {
                "baseline": {"status": "ok"},
                "fault": fault_result,
                "post": {"status": "ok"},
            },
        },
    }]
    evidence_index = {"N009": {"status": "ok", "usable": True}}
    target = {
        "service": "front-end",
        "fault_type": "http-status-code",
        "causal_category": "http-error",
    }

    projected = rebuild._target_metric_evidence(calls, evidence_index, target)

    assert [item["phase"] for item in projected] == ["baseline", "fault"]
    assert projected[1]["result"] == fault_result
    assert projected[1]["usable"]
    assert rebuild._target_metric_query_exists(calls, "http", "front-end", "fault")


def test_gate_requires_procedural_path_and_target_metric_not_experiment_effect():
    pair = {
        "effect_validated": False,
        "labels": {
            "service": "front-end",
            "fault_type": "cpu-hog",
            "causal_category": "cpu-exhaustion",
        },
    }
    guide = {
        "diagnosis": {
            "status": "confirmed",
            "primary_service": "front-end",
            "fault_type": "cpu-hog",
            "category": "cpu-exhaustion",
            "supporting_evidence": ["E1", "E2"],
            "contradicting_evidence": [],
        },
        "investigation_steps": [
            {
                "step_id": "S1",
                "kind": "observed",
                "phase": "fault",
                "service": "front-end",
                "family": "metrics",
                "evidence_ids": ["E1"],
                "observed_result": "CPU usage rose.",
                "hypothesis_ids": ["H1"],
            }
        ],
        "hypotheses": [{
            "hypothesis_id": "H1", "service": "front-end",
            "category": "cpu-exhaustion", "mechanism": "Elevated CPU",
            "supporting_evidence": ["E1"], "contradicting_evidence": [],
            "unresolved_questions": [],
        }, {
            "hypothesis_id": "H2", "service": "orders",
            "category": "dependency-failure", "mechanism": "Propagated latency",
            "supporting_evidence": [], "contradicting_evidence": ["E1"],
            "unresolved_questions": [],
        }],
        "metric_confirmation_step_id": "S1",
        "service_cause_explanation": "A latencia em orders e propagada; CPU de front-end aumentou.",
    }
    evidence_index = {
        "E1": _evidence("get_run_golden_signals", {
            "metrics": {"cpu_saturation_pct": {"top": [{
                "service": "front-end", "baseline_mean": 20, "fault_mean": 65,
            }]}},
        }),
        "E2": _evidence(
            "loki_range_query",
            {"service": "front-end", "message": "request queue grew"},
            family="logs",
        ),
    }
    review = {
        "result": "pass",
        "target_matches": True,
        "path_reaches_target": True,
        "metric_confirmation": True,
        "alternatives_explained": True,
        "unsupported_claims": [],
        "unsupported_evidence_references": [],
        "scope_violations": [],
    }

    assert rebuild._gate_reasons(
        pair, guide, review, evidence_index
    ) == []


def test_guide_requires_observed_steps_for_cited_alternative_evidence():
    guide = {
        "symptom_relation": "A latencia em orders pode vir de carts.",
        "service_cause_explanation": "A CPU alta em orders, e nao carts, e a causa.",
        "metric_confirmation_step_id": "S1",
        "hypotheses": [{
            "supporting_evidence": ["N001"], "contradicting_evidence": [],
        }],
        "investigation_steps": [{
            "step_id": "S1", "kind": "observed", "family": "metrics",
            "service": "orders", "phase": "fault",
            "evidence_ids": ["N001"], "observed_result": "CPU elevada.",
        }, {
            "step_id": "S2", "kind": "recommended", "family": "metrics",
            "service": "carts", "phase": "fault",
            "evidence_ids": [], "observed_result": None,
        }],
        "diagnosis": {
            "primary_service": "orders", "fault_type": "cpu-hog",
            "category": "cpu-exhaustion", "status": "confirmed",
            "supporting_evidence": ["N001"], "contradicting_evidence": [],
            "alternatives": [{
                "hypothesis": "CPU elevada em carts",
                "supporting_evidence": ["N004"], "contradicting_evidence": [],
            }],
        },
    }
    evidence_index = {
        "N001": {
            "service": "orders", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "usable": True,
        },
        "N004": {
            "service": "carts", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "usable": True,
        },
    }
    target = {
        "service": "orders", "fault_type": "cpu-hog",
        "causal_category": "cpu-exhaustion",
    }

    issues = rebuild._guide_issues(guide, evidence_index, target, [])

    assert any("N004" in issue and "observed" in issue for issue in issues)


def test_guide_requires_one_relevant_observed_check_per_alternative_service():
    guide = {
        "symptom_relation": "A latência de orders pode ser propagada por carts.",
        "service_cause_explanation": "A CPU elevada em orders confirma a causa.",
        "metric_confirmation_step_id": "S1",
        "hypotheses": [],
        "investigation_steps": [{
            "step_id": "S1", "kind": "observed", "family": "metrics",
            "service": "orders", "phase": "fault",
            "evidence_ids": ["N001"], "observed_result": "CPU elevada.",
        }, {
            "step_id": "S2", "kind": "observed", "family": "metrics",
            "service": "carts", "phase": "fault",
            "evidence_ids": ["N004"], "observed_result": "CPU baixa.",
        }],
        "diagnosis": {
            "primary_service": "orders", "fault_type": "cpu-hog",
            "category": "cpu-exhaustion", "status": "confirmed",
            "supporting_evidence": ["N001"], "contradicting_evidence": [],
            "alternatives": [],
        },
    }
    evidence_index = {
        "N001": {
            "service": "orders", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "usable": True,
        },
        "N004": {
            "service": "carts", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "usable": True,
        },
        "N006": {
            "service": "carts", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "usable": True,
        },
    }
    target = {
        "service": "orders", "fault_type": "cpu-hog",
        "causal_category": "cpu-exhaustion",
    }

    issues = rebuild._guide_issues(guide, evidence_index, target, [])

    assert not any("N006" in issue for issue in issues)


def test_guide_does_not_expose_private_training_label():
    guide = {
        "symptom_relation": "CPU aumentou em orders.",
        "service_cause_explanation": "O rótulo de treinamento (`private_training_target`) identifica orders.",
        "metric_confirmation_step_id": "S1",
        "hypotheses": [],
        "investigation_steps": [{
            "step_id": "S1", "kind": "observed", "family": "metrics",
            "service": "orders", "phase": "fault",
            "evidence_ids": ["N001"], "observed_result": "CPU elevada.",
        }],
        "diagnosis": {
            "primary_service": "orders", "fault_type": "cpu-hog",
            "category": "cpu-exhaustion", "status": "confirmed",
            "supporting_evidence": ["N001"], "contradicting_evidence": [],
            "alternatives": [],
        },
    }
    evidence_index = {
        "N001": {
            "service": "orders", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "usable": True,
        },
    }
    target = {
        "service": "orders", "fault_type": "cpu-hog",
        "causal_category": "cpu-exhaustion",
    }

    issues = rebuild._guide_issues(guide, evidence_index, target, [])

    assert any("private training label" in issue for issue in issues)


def test_guide_cannot_confirm_without_type_specific_target_metric():
    guide = {
        "symptom_relation": "A latência de orders aumentou.",
        "service_cause_explanation": "A causa é CPU elevada em orders.",
        "metric_confirmation_step_id": "S1",
        "hypotheses": [],
        "investigation_steps": [{
            "step_id": "S1", "kind": "observed", "family": "metrics",
            "service": "orders", "phase": "fault",
            "evidence_ids": ["N001"], "observed_result": "CPU observada.",
        }],
        "diagnosis": {
            "primary_service": "orders", "fault_type": "cpu-hog",
            "category": "cpu-exhaustion", "status": "confirmed",
            "supporting_evidence": ["N001"], "contradicting_evidence": [],
            "alternatives": [],
        },
    }
    evidence_index = {
        "N001": {
            "service": "orders", "phase": "fault", "phases": ["fault"],
            "family": "metrics", "status": "ok", "usable": True,
            "result": {"status": "ok"},
        },
    }
    target = {
        "service": "orders", "fault_type": "cpu-hog",
        "causal_category": "cpu-exhaustion",
    }

    issues = rebuild._guide_issues(guide, evidence_index, target, [])

    assert any("status=inconclusive" in issue for issue in issues)


def test_http_fi_new_series_requires_baseline_traffic_on_same_verified_edge():
    labels = {
        "source_workload": "orders", "reporter": "source",
        "destination_workload": "unknown", "destination_workload_namespace": "unknown",
        "destination_service_name": "shipping",
        "destination_service": "shipping.sock-shop.svc.cluster.local",
        "response_code": "500", "response_flags": "FI",
    }
    baseline = [{
        "labels": {**labels, "destination_workload": "shipping",
                   "destination_workload_namespace": "sock-shop",
                   "response_code": "201", "response_flags": "-"},
        "mean": 4.0, "sample_count": 5,
    }]
    result = {"purpose": "http_status_rates", "service": "shipping", "phases": {
        "baseline": {"series": baseline},
        "fault": {"series": [{"labels": labels, "mean": 3.0, "sample_count": 5}]},
    }}
    guide = {
        "diagnosis": {"supporting_evidence": ["N001"]},
        "investigation_steps": [{"kind": "observed", "phase": "fault", "service": "shipping",
                                 "family": "metrics", "evidence_ids": ["N001"]}],
    }
    index = {"N001": {"usable": True, "result": result}}
    assert rebuild._type_specific_evidence("http-status-code", "shipping", guide, index)
    result["phases"]["baseline"]["series"] = []
    assert not rebuild._type_specific_evidence("http-status-code", "shipping", guide, index)
    result["phases"]["baseline"]["series"] = baseline
    labels["destination_service"] = "shipping.other.svc.cluster.local"
    assert not rebuild._type_specific_evidence("http-status-code", "shipping", guide, index)
    result["phases"]["baseline"]["series"] = [
        {"labels": dict(labels), "mean": 0.1, "sample_count": 5},
    ]
    assert not rebuild._type_specific_evidence("http-status-code", "shipping", guide, index)


def test_unscoped_http_summary_resolves_each_service_without_scope_as_target():
    windows = {phase: {"start": "1970-01-01T00:00:01Z", "end": "1970-01-01T00:00:03Z"}
               for phase in ("baseline", "fault", "post")}
    raw = {"status": "success", "data": {"result": [
        {"metric": {
            "destination_workload": "unknown", "destination_workload_namespace": "unknown",
            "destination_service_name": service,
            "destination_service": f"{service}.sock-shop.svc.cluster.local",
            "response_code": "500", "response_flags": "FI",
        }, "values": [[2, "3"]]}
        for service in ("shipping", "payment")
    ]}}
    result = summarize_result("prometheus_range_query", raw, {}, windows, kind="http")
    assert result["status"] == "ok"
    assert result["service"] is None
    assert {row["service"] for row in result["phases"]["fault"]["series"]} == {"shipping", "payment"}


def test_unlinked_evidence_references_are_audited_and_pruned_safely():
    guide = {
        "diagnosis": {
            "status": "confirmed",
            "supporting_evidence": ["N001", "N002"],
            "contradicting_evidence": ["N004"],
            "alternatives": [{
                "supporting_evidence": ["N001"],
                "contradicting_evidence": [],
            }],
        },
        "hypotheses": [{
            "supporting_evidence": ["N003"],
            "contradicting_evidence": ["N001"],
        }],
        "investigation_steps": [{
            "kind": "observed",
            "evidence_ids": ["N002", "N003"],
        }],
    }

    removed = rebuild._prune_unlinked_evidence_references(guide)

    assert guide["diagnosis"]["supporting_evidence"] == ["N002"]
    assert guide["diagnosis"]["contradicting_evidence"] == []
    assert guide["diagnosis"]["alternatives"][0]["supporting_evidence"] == []
    assert guide["hypotheses"][0]["supporting_evidence"] == ["N003"]
    assert guide["hypotheses"][0]["contradicting_evidence"] == []
    assert removed == [
        {
            "path": "diagnosis.supporting_evidence",
            "evidence_ids": ["N001"],
        },
        {
            "path": "diagnosis.contradicting_evidence",
            "evidence_ids": ["N004"],
        },
        {
            "path": "diagnosis.alternatives[0].supporting_evidence",
            "evidence_ids": ["N001"],
        },
        {
            "path": "hypotheses[0].contradicting_evidence",
            "evidence_ids": ["N001"],
        },
    ]


def test_metric_confirmation_pointer_uses_an_observed_target_fault_step():
    guide = {
        "metric_confirmation_step_id": "S1",
        "investigation_steps": [{
            "step_id": "S1", "family": "metrics", "service": "orders",
            "phase": "fault", "kind": "observed",
        }, {
            "step_id": "S2", "family": "metrics", "service": "shipping",
            "phase": "fault", "kind": "recommended",
        }, {
            "step_id": "S3", "family": "metrics", "service": "shipping",
            "phase": "fault", "kind": "observed",
            "observed_result": "Status 503 permaneceu em zero.",
            "evidence_ids": ["N003"],
        }],
    }

    change = rebuild._normalize_metric_confirmation_pointer(
        guide,
        {"service": "shipping", "fault_type": "http-status-code"},
    )

    assert change == {"from": "S1", "to": "S3"}
    assert guide["metric_confirmation_step_id"] == "S3"


def test_metric_confirmation_pointer_prefers_the_observed_target_metric_evidence():
    guide = {
        "metric_confirmation_step_id": "S1",
        "investigation_steps": [{
            "step_id": "S1", "family": "metrics", "service": "user",
            "phase": "fault", "kind": "observed", "evidence_ids": ["N001"],
            "observed_result": "Golden signals mostram degradação.",
        }, {
            "step_id": "S2", "family": "metrics", "service": "user",
            "phase": "fault", "kind": "observed", "evidence_ids": ["N003"],
            "observed_result": "A métrica de memória mostra o valor coletado.",
        }],
    }

    change = rebuild._normalize_metric_confirmation_pointer(
        guide,
        {"service": "user", "fault_type": "memory-hog"},
        {"N003"},
    )

    assert change == {"from": "S1", "to": "S2"}
    assert guide["metric_confirmation_step_id"] == "S2"


def test_metric_confirmation_pointer_accepts_restart_family_for_container_kill():
    guide = {
        "metric_confirmation_step_id": "S2",
        "investigation_steps": [{
            "step_id": "S1", "family": "lifecycle", "service": "user",
            "phase": "fault", "kind": "observed", "evidence_ids": ["N002"],
            "observed_result": "Um restart foi observado no container user.",
        }, {
            "step_id": "S2", "family": "metrics", "service": "orders",
            "phase": "fault", "kind": "observed", "evidence_ids": ["N004"],
            "observed_result": "CPU de orders permaneceu estável.",
        }],
    }

    change = rebuild._normalize_metric_confirmation_pointer(
        guide,
        {"service": "user", "fault_type": "container-kill"},
    )

    assert change == {"from": "S2", "to": "S1"}
    assert guide["metric_confirmation_step_id"] == "S1"


def test_historical_queries_are_mapped_to_phase_by_timestamp():
    phases = {
        "fault": {
            "start": "2026-11-04T10:07:22.018774+00:00",
            "end": "2026-11-04T10:07:25.155830+00:00",
        }
    }
    row = {
        "arguments": {
            "start": "2026-11-04T10:07:22.018774Z",
            "end": "2026-11-04T10:07:25.155830Z",
        }
    }

    assert rebuild._row_phases(row, phases) == ["fault"]


def test_evidence_phase_validation_uses_all_returned_range_query_phases():
    evidence = {
        "phase": "baseline",
        "phases": ["baseline", "fault", "post"],
    }

    assert rebuild._evidence_covers_phase(evidence, "fault")
    assert not rebuild._evidence_covers_phase(evidence, "unknown")
    assert rebuild._evidence_covers_phase({"phase": "fault"}, "fault")
    assert not rebuild._evidence_covers_phase({"phase": "baseline"}, "fault")


def test_model_json_uses_the_strict_output_schema(monkeypatch):
    class Output(rebuild.StrictModel):
        answer: str

    captured = {}

    def fake_call(messages, tools, **kwargs):
        captured.update(kwargs)
        return {"content": '{"answer":"valid"}'}

    monkeypatch.setattr(rebuild, "call_llm_tool_messages", fake_call)

    parsed, attempts, _ = rebuild._model_json(
        "test_stage",
        Output,
        {},
        services=[],
        fault_types=[],
        categories=[],
        evidence_ids=[],
        max_tokens=50,
    )

    assert parsed == {"answer": "valid"}
    assert attempts[0]["model"] == rebuild.QWEN35_MODEL_ID
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["name"] == "test_stage"
    schema = captured["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False


def test_model_json_defers_semantic_issues_explicitly_for_auditor(monkeypatch):
    class Output(rebuild.StrictModel):
        answer: str
    calls = []
    def fake_call(*args, **kwargs):
        calls.append(kwargs)
        return {"content": '{"answer":"needs correction"}'}
    monkeypatch.setattr(rebuild, "call_llm_tool_messages", fake_call)
    parsed, attempts, _ = rebuild._model_json(
        "deferred_author", Output, {}, services=[], fault_types=[],
        categories=[], evidence_ids=[], max_tokens=50,
        defer_validation_issues=True,
        validate=lambda value: ["Unsupported claim"],
    )
    assert parsed["answer"] == "needs correction"
    assert len(calls) == 1
    assert attempts[0]["deferred_validation_issues"] == ["Unsupported claim"]


def _step(**updates):
    return {
        "step_id": "S1", "kind": "observed", "family": "metrics",
        "service": "user", "phase": "fault", "objective": "Comparar memoria",
        "verification": "Consultar memoria baseline/fault",
        "observed_result": "Memoria passou de 19.25% a 55.0586%",
        "interpretation": "Crescimento no servico candidato",
        "expected_support": "Memoria elevada", "expected_refutation": "Memoria estavel",
        "evidence_ids": ["N001"], "hypothesis_ids": ["H1"], "next_step_id": None,
        **updates,
    }


@pytest.mark.parametrize("updates", [
    {"observed_result": None}, {"evidence_ids": []},
    {"kind": "recommended"}, {"kind": "recommended", "evidence_ids": []},
])
def test_step_rejects_missing_results_and_recommendations_with_observations(updates):
    with pytest.raises(ValidationError):
        rebuild.GuidedStep.model_validate(_step(**updates))


def test_semantic_projection_preserves_every_service_and_numeric_fact():
    raw = {"status": "ok", "metrics": {
        "memory_saturation_pct": {
            "status": "ok", "unit": "%", "top": [
                {"service": service, "baseline_mean": 19.25, "fault_mean": 55.0586}
                for service in sorted(rebuild.TARGET_SERVICES)
            ],
        },
    }}
    summary = summarize_result("get_run_golden_signals", raw, {}, {})
    projected = evidence_context([{
        "id": "N001", "tool": "get_run_golden_signals", "digest": summary,
        "arguments": {}, "family": "metrics", "status": "ok", "source": "history",
        "observation_count": 7, "fault_observation_count": 7,
    }])
    encoded = json.dumps(projected)
    assert "55.0586" in encoded
    for service in rebuild.TARGET_SERVICES:
        assert service in encoded
    assert "_truncated" not in encoded


def test_type_gate_requires_numeric_comparison_not_a_cpu_keyword():
    guide = {"diagnosis": {"supporting_evidence": ["N001"]}}
    evidence = {"N001": _evidence("get_run_golden_signals", {
        "service": "user", "metrics": {
            "memory_saturation_pct": {"top": [{
                "service": "user", "baseline_mean": 19.25, "fault_mean": 55.0586,
            }]},
        },
    })}
    assert rebuild._type_specific_evidence("memory-hog", "user", guide, evidence)
    assert not rebuild._type_specific_evidence("cpu-hog", "user", guide, evidence)


def test_cpu_gate_uses_numeric_baseline_even_when_found_flag_is_false():
    guide = {"diagnosis": {"supporting_evidence": ["N004", "N005"]}}
    fault = _evidence("get_cpu_saturation_top", {
        "status": "ok",
        "top": [{"service": "orders", "saturation_mean_pct": 66.34}],
    })
    fault["phase"] = "fault"
    baseline = _evidence("get_cpu_saturation_top", {
        "status": "ok",
        "found": False,
        "count": 0,
        "top": [{"service": "orders", "saturation_mean_pct": 17.4}],
    })
    baseline["phase"] = "baseline"
    baseline["usable"] = False

    evidence = {"N004": fault, "N005": baseline}

    assert rebuild._type_specific_evidence("cpu-hog", "orders", guide, evidence)


def test_restart_in_sidecar_does_not_prove_target_restart():
    item = _evidence("check_pod_restarts", {
        "affected": [{
            "service": "user", "restarts_in_window": 1, "pod_observations": [{
                "container": "istio-proxy",
                "restart_observations": [{"restart_count_delta": 1}],
            }],
        }],
    })
    assert not rebuild._positive_target_restart(item, "user")


def test_semantic_contract_failure_gets_one_repair(monkeypatch):
    class Output(rebuild.StrictModel):
        answer: str

    responses = iter(['{"answer":"unsupported"}', '{"answer":"supported"}'])
    calls = []

    def fake_call(messages, tools, **kwargs):
        calls.append(list(messages))
        return {"content": next(responses)}

    monkeypatch.setattr(rebuild, "call_llm_tool_messages", fake_call)
    value, attempts, _ = rebuild._model_json(
        "test_semantic_repair", Output, {}, services=[], fault_types=[],
        categories=[], evidence_ids=[], max_tokens=50,
        validate=lambda output: [] if output["answer"] == "supported" else ["missing evidence"],
    )
    assert value["answer"] == "supported"
    assert len(attempts) == 2
    assert "missing evidence" in attempts[0]["validation_error"]
    assert attempts[0]["content"] == '{"answer":"unsupported"}'


def test_model_json_allows_evidence_checked_confirmation_repair(monkeypatch):
    class Output(rebuild.StrictModel):
        diagnosis: dict[str, str]

    responses = iter([
        '{"diagnosis":{"status":"inconclusive"}}',
        '{"diagnosis":{"status":"confirmed"}}',
    ])

    def fake_call(messages, tools, **kwargs):
        return {"content": next(responses)}

    monkeypatch.setattr(rebuild, "call_llm_tool_messages", fake_call)
    parsed, attempts, _ = rebuild._model_json(
        "write_training_b", Output, {},
        services=[], fault_types=[], categories=[], evidence_ids=[], max_tokens=50,
        allow_confirmation_repair=True,
        validate=lambda output: (
            [] if output["diagnosis"]["status"] == "confirmed"
            else ["available target metric supports the supervised diagnosis"]
        ),
    )

    assert parsed["diagnosis"]["status"] == "confirmed"
    assert len(attempts) == 2


def test_historical_query_persists_response_hash_and_does_not_invent_observations(tmp_path):
    class Client:
        def list_tools(self):
            return [{"name": "get_memory_saturation_top"}]

        def call_tool_json(self, tool, arguments, **kwargs):
            return {
                "status": "ok", "top": [{"service": "user", "saturation_mean_pct": 55.0586}],
                "coverage": {"sample_count": 13},
            }

    windows = {
        phase: {"start": "2026-11-04T10:00:00Z", "end": "2026-11-04T10:01:00Z"}
        for phase in ("baseline", "fault", "post")
    }
    session = HistoricalQueries(Client(), windows, tmp_path)
    item = session.query(
        "get_memory_saturation_top", windows["fault"], "metrics",
        service="user", phase="fault", kind="memory",
    )
    assert item["fault_observation_count"] == 13
    assert rebuild._file_sha256(tmp_path / "N001.json") == item["result_sha256"]
    assert session.index()["N001"]["usable"]


def test_non_finite_prometheus_result_is_recorded_as_unusable_evidence(tmp_path):
    class Client:
        def list_tools(self):
            return [{"name": "prometheus_range_query"}]

        def call_tool_json(self, tool, arguments, **kwargs):
            return {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [{
                        "metric": {"response_code": "503"},
                        "values": [[1793790000, "NaN"]],
                    }],
                },
            }

    windows = {
        "baseline": {"start": "2026-11-04T10:00:00Z", "end": "2026-11-04T10:01:00Z"},
        "fault": {"start": "2026-11-04T10:02:00Z", "end": "2026-11-04T10:03:00Z"},
        "post": {"start": "2026-11-04T10:04:00Z", "end": "2026-11-04T10:05:00Z"},
    }
    session = HistoricalQueries(Client(), windows, tmp_path)

    item = session.query(
        "prometheus_range_query",
        {"query": "sum(rate(istio_requests_total[1m]))", **windows["fault"]},
        "metrics",
        service="shipping",
        phase="fault",
        kind="http",
    )

    assert item["status"] == "tool_error"
    assert item["digest"]["error"] == "Non-finite Prometheus sample"
    assert not session.index()["N001"]["usable"]


def test_saved_trace_phase_is_recovered_by_exact_trace_id():
    pair = {"phases": {}, "evidence": [{
        "id": "E001", "tool": "tempo_analyze_trace", "family": "traces",
        "status": "ok", "fault_observation_count": 2,
        "arguments": {"trace_id": "exact-id"},
        "digest": {"trace_id": "exact-id", "phase": "fault", "span_count": 2},
    }]}
    _, index = rebuild._historical_evidence(pair, [{
        "query_index": 1, "tool": "tempo_analyze_trace", "status": "response_received",
        "arguments": {"trace_id": "exact-id"}, "result": {"span_count": 2},
    }])
    assert index["H001"]["phases"] == ["fault"]
    assert index["H001"]["usable"]


def test_supervised_pipeline_uses_label_for_queries_and_requires_target_metric(monkeypatch, tmp_path):
    windows = {
        "baseline": {"start": "2026-11-04T10:00:00Z", "end": "2026-11-04T10:01:00Z"},
        "fault": {"start": "2026-11-04T10:02:00Z", "end": "2026-11-04T10:03:00Z"},
        "post": {"start": "2026-11-04T10:04:00Z", "end": "2026-11-04T10:05:00Z"},
    }
    queries = []

    class Client:
        sse_url = "http://127.0.0.1:18082/sse"

        def list_tools(self):
            return [{"name": name, "inputSchema": {}} for name in (
                "get_run_golden_signals", "check_pod_restarts",
                "get_memory_saturation_top", "loki_range_query",
            )]

        def call_tool_json(self, tool, arguments, **kwargs):
            queries.append((tool, arguments))
            if tool == "get_run_golden_signals":
                return {"status": "ok", "metrics": {"memory_saturation_pct": {
                    "status": "ok", "unit": "%",
                    "phases": {"fault": {"sample_count": 10}},
                    "top": [{"service": "user", "baseline_mean": 19.25, "fault_mean": 55.0586}],
                }}}
            if tool == "check_pod_restarts":
                return {"status": "ok", "count": 0, "coverage": {"sample_count": 10}}
            if tool == "get_memory_saturation_top":
                mean = 19.25 if arguments["start"] == windows["baseline"]["start"] else 55.0586
                return {"status": "ok", "unit": "%", "coverage": {"sample_count": 10},
                        "top": [{"service": "user", "saturation_mean_pct": mean,
                                 "sample_count": 10}]}
            return {"status": "ok", "service": "user", "phases": {"fault": {
                "coverage": {"lines_returned": 1},
                "sample_lines": ["user allocation pressure during login"],
            }}}

    hypotheses = [{
        "hypothesis_id": identifier, "service": "user",
        "category": category, "mechanism": mechanism,
        "supporting_evidence": ["N001", "N005"], "contradicting_evidence": [],
        "unresolved_questions": [],
    } for identifier, category, mechanism in (
        ("H1", "memory-exhaustion", "Pressao de memoria"),
        ("H2", "cpu-exhaustion", "Consumo de CPU"),
    )]
    diagnosis = {
        "status": "confirmed", "primary_service": "user",
        "category": "memory-exhaustion", "fault_type": "memory-hog",
        "mechanism": "Crescimento de memoria e pressao de alocacao",
        "supporting_evidence": ["N001", "N003", "N004", "N005"],
        "contradicting_evidence": [],
        "alternatives": [
            {"hypothesis": "CPU elevada", "supporting_evidence": [], "contradicting_evidence": []},
            {"hypothesis": "Latencia de rede", "supporting_evidence": [], "contradicting_evidence": []},
        ],
        "causal_chain": ["Aumento de memoria -> pressao de alocacao -> latencia"],
        "restart_analysis": [], "limitations": [], "next_checks": [],
    }
    guide = {
        "symptom_relation": "A latencia de A e investigada ate a pressao de memoria",
        "service_cause_explanation": "A memoria aumentou no user; latencia downstream e efeito propagado.",
        "metric_confirmation_step_id": "S2",
        "hypotheses": hypotheses,
        "investigation_steps": [
            _step(
                step_id="S1", phase="baseline", evidence_ids=["N004"],
                observed_result="Memoria no user: 19,25% baseline.",
                next_step_id="S2",
            ),
            _step(
                step_id="S2", evidence_ids=["N001", "N005"],
                observed_result="Memoria no user: 19,25% baseline e 55,0586% fault.",
                next_step_id="S3",
            ),
            _step(
                step_id="S3", family="logs", evidence_ids=["N003"],
                observed_result="user allocation pressure during login",
            ),
        ],
        "diagnosis": diagnosis, "limitations": [],
    }
    seen_contexts = []
    training_schemas = []

    def fake_llm(messages, tools, **kwargs):
        request = json.loads(messages[1]["content"])
        stage, context = request["stage"], request["context"]
        seen_contexts.append((stage, context))
        if stage == "write_training_b":
            training_schemas.append(request["output_schema"])
        if stage == "plan_historical_checks":
            result = {
                "hypotheses": hypotheses, "reason": "Corroborar memoria com logs",
                "stop": False, "checks": [{
                    "kind": "logs", "service": "user", "phase": "fault",
                    "hypothesis": "H1", "expected_support": "Pressao de alocacao",
                    "expected_refutation": "Sem pressao", "historical_pair_ids": [],
                    "patterns": [],
                }],
            }
        elif stage == "write_training_b":
            result = guide
        elif stage == "repair_factual_guide":
            result = guide
        elif stage in {"review_training_b", "review_repaired_training_b"}:
            passed = stage == "review_repaired_training_b"
            result = {
                "result": "pass" if passed else "fail",
                "target_matches": True, "unsupported_claims": [],
                "path_reaches_target": True, "metric_confirmation": True,
                "alternatives_explained": passed,
                "unsupported_evidence_references": [], "scope_violations": [],
                "rationale": "Valores e cadeia conferidos" if passed else "Explique a alternativa",
            }
        else:
            raise AssertionError(stage)
        return {"content": json.dumps(result)}

    monkeypatch.setattr(rebuild, "call_llm_tool_messages", fake_llm)
    pair = {
        "pair_id": "pair", "source_run_id": "run-train", "sha256": "a" * 64,
        "effect_validated": False, "phases": windows,
        "a": {"symptoms": ["Latencia durante login"]},
        "labels": {"service": "user", "fault_type": "memory-hog", "causal_category": "memory-exhaustion"},
    }
    result = rebuild._case_result(pair, "case-test", Client(), tmp_path)
    assert result["status"] == "procedurally_validated"
    assert result["gate_reasons"] == []
    assert len(result["revisions"]) == 1
    assert sum(tool == "loki_range_query" for tool, _ in queries) == 1
    assert len(result["supervised_investigation"]["skipped_duplicate_checks"]) == 1
    assert sum(tool == "get_memory_saturation_top" for tool, _ in queries) == 2
    executed = next(
        query
        for step in result["guide"]["investigation_steps"]
        for query in step["executed_queries"]
        if query["evidence_id"] == "N003"
    )
    assert executed["evidence_id"] == "N003"
    query = next(
        item for item in result["collection_evidence"] if item["id"] == "N003"
    )
    assert executed["tool"] == query["tool"]
    assert 'container=~"user|istio-proxy"' in query["arguments"]["query"]
    assert query["result_sha256"] == rebuild._file_sha256(
        tmp_path / "supervised_investigation/queries/N003.json"
    )
    planning_contexts = [
        context for stage, context in seen_contexts
        if stage == "plan_historical_checks"
    ]
    assert planning_contexts
    assert planning_contexts[0]["required_search_destination"] == pair["labels"]
    assert planning_contexts[0]["rules"]
    writing_contexts = [
        context for stage, context in seen_contexts
        if stage == "write_training_b"
    ]
    assert writing_contexts[0]["target_metric_evidence"]
    assert "private_training_target_not_evidence" not in writing_contexts[0]
    assert "N005" in {
        item["evidence_id"] for item in writing_contexts[0]["target_metric_evidence"]
    }
    diagnosis_properties = training_schemas[0]["$defs"]["TrainingDiagnosis"]["properties"]
    assert diagnosis_properties["primary_service"]["enum"] == ["user"]
    assert diagnosis_properties["fault_type"]["enum"] == ["memory-hog"]
    assert diagnosis_properties["category"]["enum"] == ["memory-exhaustion"]
    assert result["guide"]["metric_confirmation_step_id"] == "S2"
    assert result["collection_evidence"][-2]["phase"] == "baseline"
    assert result["collection_evidence"][-1]["phase"] == "fault"


def test_query_transport_retry_is_bounded_and_preserved(tmp_path):
    class Client:
        calls = 0

        def list_tools(self):
            return [{"name": "check_pod_restarts"}]

        def call_tool_json(self, tool, arguments, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("historical query timed out")
            return {"status": "ok", "count": 0, "coverage": {"sample_count": 1}}

    windows = {
        phase: {"start": "2026-11-04T10:00:00Z", "end": "2026-11-04T10:01:00Z"}
        for phase in ("baseline", "fault", "post")
    }
    client = Client()
    session = HistoricalQueries(client, windows, tmp_path)
    session.query("check_pod_restarts", windows["fault"], "metrics", phase="fault")
    assert client.calls == 2
    failure = json.loads((tmp_path / "N001-attempt-1-error.json").read_text())
    assert failure["status"] == "collection_error"
    response = json.loads((tmp_path / "N001.json").read_text())
    assert response["transport_attempts"] == [failure]


def test_failed_pilot_is_explicitly_non_publishable(tmp_path):
    failure = {
        "status": "collection_error", "stage": "preflight",
        "error": "historical endpoint unavailable",
    }
    rebuild._smoke_failure_report(
        tmp_path, {"source": "a" * 64}, "http://127.0.0.1:18082/sse", failure,
    )
    report = json.loads((tmp_path / "pilot.json").read_text())
    assert report["pipeline_checks_passed"] is False
    assert report["failure"] == failure
    assert report["producer_sha256"] == rebuild._producer_sha256()


def test_full_rebuild_needs_explicit_approval_for_incomplete_pilot():
    expected_cases = {
        "case-kill": "container-kill",
        "case-cpu": "cpu-hog",
        "case-memory": "memory-hog",
        "case-network": "network-latency",
        "case-http": "http-status-code",
    }
    source_hashes = {"source_base_sha256": "a" * 64}
    report = {
        "status": "pilot_completed",
        "source_split": "train",
        "producer_sha256": "b" * 64,
        "source_hashes": source_hashes,
        "historical_mcp_url": "http://127.0.0.1:18082/sse",
        "selected_case_count": 5,
        "completed_case_count": 4,
        "failed_case_count": 1,
        "pipeline_checks_passed": False,
        "status_counts": {
            "procedurally_validated": 2,
            "procedural_pending": 2,
        },
        "cases": [
            {"case_id": "case-kill", "fault_type": "container-kill",
             "status": "procedurally_validated"},
            {"case_id": "case-memory", "fault_type": "memory-hog",
             "status": "procedurally_validated"},
            {"case_id": "case-network", "fault_type": "network-latency",
             "status": "procedural_pending"},
            {"case_id": "case-http", "fault_type": "http-status-code",
             "status": "procedural_pending"},
        ],
        "failures": [{"case_id": "case-cpu", "status": "validation_error"}],
    }
    arguments = {
        "producer_hash": "b" * 64,
        "source_hashes": source_hashes,
        "historical_mcp_url": "http://127.0.0.1:18082/sse",
        "expected_cases": expected_cases,
    }

    with pytest.raises(ValueError, match="explicit approval"):
        rebuild._validate_full_rebuild_pilot(
            report, **arguments, continue_after_incomplete=False
        )

    rebuild._validate_full_rebuild_pilot(
        report, **arguments, continue_after_incomplete=True
    )
    report["source_hashes"] = {"source_base_sha256": "c" * 64}
    with pytest.raises(ValueError, match="cohort or provenance mismatch"):
        rebuild._validate_full_rebuild_pilot(
            report, **arguments, continue_after_incomplete=True
        )


def test_trace_graph_projection_preserves_phase_counts_for_corroboration():
    raw = {"phases": {
        "baseline": {"trace_count": 4, "top_nodes": [], "top_edges": []},
        "fault": {"trace_count": 3, "top_nodes": [], "top_edges": []},
        "post": None,
    }, "delta": {}}
    result = summarize_result("tempo_service_graph_phases", raw, {}, {})
    assert result["status"] == "ok"
    assert result["phases"]["fault"]["trace_count"] == 3


def test_host_derives_next_step_links_without_changing_observed_facts():
    hypotheses = [{
        "hypothesis_id": identifier, "service": "user", "category": "memory-exhaustion",
        "mechanism": "Comparar recursos", "supporting_evidence": [],
        "contradicting_evidence": [], "unresolved_questions": [],
    } for identifier in ("H1", "H2")]
    guide = rebuild.TrainingGuide.model_validate({
        "symptom_relation": "Investigar a latencia de A",
        "service_cause_explanation": "A metrica de memoria no user discrimina a origem.",
        "metric_confirmation_step_id": "S1",
        "hypotheses": hypotheses,
        "investigation_steps": [
            _step(next_step_id="S99"), _step(step_id="S2", next_step_id="S1"),
        ],
        "diagnosis": {
            "status": "inconclusive", "primary_service": "user",
            "category": "memory-exhaustion", "fault_type": "memory-hog",
            "mechanism": "Ainda falta corroboracao", "supporting_evidence": ["N001"],
            "contradicting_evidence": [], "causal_chain": [], "restart_analysis": [],
            "limitations": [], "next_checks": [],
            "alternatives": [{
                "hypothesis": hypothesis, "supporting_evidence": [],
                "contradicting_evidence": [],
            } for hypothesis in ("CPU", "Rede")],
        },
        "limitations": [],
    })
    assert [step.next_step_id for step in guide.investigation_steps] == ["S2", None]
    assert all(
        step.observed_result == "Memoria passou de 19.25% a 55.0586%"
        for step in guide.investigation_steps
    )





def test_training_guide_accepts_lifecycle_confirmation_for_container_kill():
    hypotheses = [{
        "hypothesis_id": identifier, "service": "user", "category": "pod-failure",
        "mechanism": "Verificar o reinício do container",
        "supporting_evidence": [], "contradicting_evidence": [],
        "unresolved_questions": [],
    } for identifier in ("H1", "H2")]
    guide = rebuild.TrainingGuide.model_validate({
        "symptom_relation": "Erros downstream coincidem com o restart de user.",
        "service_cause_explanation": "A métrica de lifecycle confirma o restart no alvo.",
        "metric_confirmation_step_id": "S1",
        "hypotheses": hypotheses,
        "investigation_steps": [
            _step(
                family="lifecycle",
                verification="Consultar check_pod_restarts para user.",
                observed_result="O container user reiniciou uma vez.",
                evidence_ids=["N002"],
            ),
            _step(
                step_id="S2",
                service="orders",
                evidence_ids=["N001"],
                hypothesis_ids=["H2"],
            ),
        ],
        "diagnosis": {
            "status": "inconclusive", "primary_service": "user",
            "category": "pod-failure", "fault_type": "container-kill",
            "mechanism": "O restart está confirmado, sem motivo de saída disponível.",
            "supporting_evidence": ["N002"], "contradicting_evidence": [],
            "causal_chain": [], "restart_analysis": [], "limitations": [],
            "next_checks": [],
            "alternatives": [{
                "hypothesis": hypothesis, "supporting_evidence": [],
                "contradicting_evidence": [],
            } for hypothesis in ("Rede", "Falha local")],
        },
        "limitations": [],
    })

    assert guide.metric_confirmation_step_id == "S1"
    assert guide.investigation_steps[0].family == "lifecycle"
