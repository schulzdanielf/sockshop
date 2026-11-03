from __future__ import annotations

import json
from pathlib import Path

import pytest

from experiment.eval.memory_hog import frozen_rca_replay as replay
from experiment.platform.backend import rca_knowledge
from experiment.platform.backend.rca_knowledge import IncidentA


def frozen_inputs():
    observation = {
        "description": "CPU saturation increased during the fault phase.",
        "evidence_ids": ["E1"],
    }
    a = IncidentA.model_validate(
        {
            "observed_services": ["front-end"],
            "service_relations": [],
            "symptoms": [observation],
            "metric_changes": [observation],
            "log_patterns": [],
            "trace_patterns": [],
            "temporal_sequence": [],
            "preliminary_hypotheses": ["DO_NOT_LEAK_THIS_HYPOTHESIS"],
            "limitations": ["Trace coverage is incomplete."],
        }
    )
    evidence = [
        {
            "id": "E1",
            "tool": "prometheus_range_query",
            "family": "metrics",
            "status": "ok",
            "observation_count": 1,
            "arguments": {"query": "cpu"},
            "digest": {"summary": "CPU changed"},
        }
    ]
    return a, evidence


def test_frozen_replay_entry_points_are_disabled(tmp_path, capsys):
    with pytest.raises(
        replay.campaign_knowledge.EvaluationError,
        match="Frozen-evidence RCA replay is disabled",
    ):
        replay.run_replay(
            campaign_dir=tmp_path / "campaign",
            retry_dir=tmp_path / "retry",
            object_store=tmp_path / "store",
            output=tmp_path / "output",
            call=None,
            settings={},
        )

    assert replay.main(["--output-dir", str(tmp_path / "output")]) == 2
    assert "disabled" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


def test_frozen_incident_and_three_conditions_do_not_leak_predictions_or_labels():
    a, evidence = frozen_inputs()
    incident = replay.build_incident(a, evidence, {"front-end"})
    history = [
        {
            "pair_id": "historical-pair",
            "historical_status": "unconfirmed hypothesis",
            "source_effect_observed": True,
            "observations": [{"kind": "symptoms", "description": "Latency increased."}],
            "symptom_relation": "Historical symptom relation.",
            "investigation_steps": [],
            "applicability": [],
            "recommended_checks": ["Compare service CPU by phase."],
            "limitations": [],
        }
    ]
    skill = "Retrospective analysis instructions."
    contract = {
        "known_services": ["front-end"],
        "possible_fault_types": ["cpu-hog"],
        "allowed_categories": ["cpu-exhaustion", "unknown"],
    }
    messages = {
        condition: replay.build_messages(
            skill, contract, incident, "Sock Shop system card", condition, history
        )
        for condition in replay.CONDITIONS
    }
    payloads = {
        condition: json.loads(messages[condition][-1]["content"])
        for condition in replay.CONDITIONS
    }

    assert all(payload["incident"] == incident for payload in payloads.values())
    assert "application_context" not in payloads["skill"]
    assert payloads["control"]["application_context"] == "Sock Shop system card"
    assert "past_experiences" not in payloads["control"]
    assert payloads["rag"]["past_experiences"] == history
    assert "DO_NOT_LEAK_THIS_HYPOTHESIS" not in json.dumps(messages)
    assert all(
        "ground_truth_service" not in json.dumps(condition_messages)
        and "ground_truth_fault" not in json.dumps(condition_messages)
        for condition_messages in messages.values()
    )


def test_rag_prompt_rejects_unfiltered_historical_labels():
    a, evidence = frozen_inputs()
    incident = replay.build_incident(a, evidence, {"front-end"})

    with pytest.raises(ValueError, match="historical labels or diagnoses"):
        replay.build_messages(
            "skill",
            {},
            incident,
            "card",
            "rag",
            [{"pair_id": "pair", "labels": {"fault_type": "cpu-hog"}}],
        )


def test_oracle_top3_forces_unvalidated_exact_pair_and_excludes_unmatched(
    monkeypatch,
):
    a, evidence = frozen_inputs()

    def pair(pair_id, service, fault_type, effect_validated):
        return {
            "pair_id": pair_id,
            "source_run_id": f"train-{pair_id}",
            "labels": {"service": service, "fault_type": fault_type},
            "effect_validated": effect_validated,
            "a": a.model_dump(),
            "b": {
                "symptom_relation": "Historical symptom relation.",
                "investigation_steps": [],
                "applicability": [],
                "recommended_checks": ["Compare the metric by phase."],
                "limitations": [],
            },
        }

    exact = pair("exact-unvalidated", "front-end", "cpu-hog", False)
    neighbor = pair("validated-neighbor", "carts", "memory-hog", True)
    retrieve_calls = []

    def retrieve(*args, **kwargs):
        retrieve_calls.append(kwargs)
        return [neighbor]

    monkeypatch.setattr(replay, "retrieve_pairs", retrieve)
    evaluation = {
        "per_run": [
            {
                "cell_id": "matched-case",
                "run_status": "completed",
                "ground_truth_service": "front-end",
                "chaos_type": "cpu-hog",
            },
            {
                "cell_id": "unmatched-case",
                "run_status": "completed",
                "ground_truth_service": "orders",
                "chaos_type": "http-status-code",
            },
        ]
    }
    responses = {
        case_id: {
            "status": "inconclusive",
            "source_run_id": f"test-{case_id}",
            "a": a.model_dump(),
            "evidence": evidence,
        }
        for case_id in ("matched-case", "unmatched-case")
    }
    contract = {
        "known_services": ["front-end", "carts", "orders"],
        "possible_fault_types": ["cpu-hog", "memory-hog", "http-status-code"],
    }

    cases, excluded = replay._prepare_cases(
        evaluation,
        responses,
        {"pairs": [exact, neighbor]},
        contract,
        a_text_version=rca_knowledge.LEGACY_A_TEXT_VERSION,
        retrieval_policy="oracle_exact_pair_in_top3",
    )

    assert len(cases) == 1
    assert cases[0]["case_id"] == "matched-case"
    assert [pair["pair_id"] for pair in cases[0]["retrieved"]] == [
        "exact-unvalidated",
        "validated-neighbor",
    ]
    assert cases[0]["oracle_pair_id"] == "exact-unvalidated"
    assert cases[0]["oracle_pair_effect_validated"] is False
    assert cases[0]["history"][0]["source_effect_validated"] is False
    assert all("labels" not in item for item in cases[0]["history"])
    assert retrieve_calls == [
        {
            "top_k": 3,
            "min_score": 0.05,
            "exploratory": True,
            "include_unvalidated_experimental": True,
        }
    ]
    assert excluded == [
        {"case_id": "unmatched-case", "reason": "no_matching_training_pair"}
    ]

    results = []
    for condition in replay.CONDITIONS:
        correct = condition == "rag"
        results.append(
            {
                "case_id": "matched-case",
                "condition": condition,
                "joint_correct": correct,
                "service_correct": correct,
                "fault_type_correct": correct,
                "diagnosis": {
                    "status": "inconclusive",
                    "primary_service": "front-end",
                    "fault_type": "cpu-hog",
                },
            }
        )
    report = replay._summarize(
        results,
        cases,
        {"sha256": "base-sha", "pairs": [exact, neighbor]},
        "input-sha",
        "oracle_exact_pair_in_top3",
        excluded,
    )
    assert report["case_count"] == 1
    assert report["retrieval_policy"] == "oracle_exact_pair_in_top3"
    assert report["retrieval_summary"]["forced_pairs_with_unvalidated_effect"] == 1
    assert report["rag_vs_skill_joint"] == {
        "rag_wins": 1,
        "skill_wins": 0,
        "ties": 0,
    }


def test_opensearch_replay_uses_indexed_history_without_oracle_exclusion(monkeypatch):
    a, evidence = frozen_inputs()
    history = {
        "pair_id": "indexed-pair",
        "historical_status": "unconfirmed hypothesis; not current-run evidence",
        "source_effect_validated": False,
        "observations": [{"kind": "symptoms", "description": "CPU increased."}],
        "symptom_relation": "Historical symptom relation.",
        "investigation_steps": [],
        "applicability": [],
        "recommended_checks": ["Compare service CPU by phase."],
        "limitations": [],
    }

    class Provider:
        def embed(self, text):
            assert "ground_truth" not in text
            return "query-vector"

    class Index:
        calls = []

        def search(self, query_text, query_vector, **kwargs):
            self.calls.append(
                {"query_text": query_text, "query_vector": query_vector, **kwargs}
            )
            return [
                {
                    "pair_id": "indexed-pair",
                    "source_run_id": "train-run",
                    "effect_validated": False,
                    "guide_status": "pending",
                    "causal_status": "inconclusive",
                    "history": history,
                }
            ]

    index = Index()
    monkeypatch.setattr(replay, "get_embedding_provider", lambda: Provider())
    evaluation = {
        "per_run": [
            {
                "cell_id": "matched-case",
                "run_status": "completed",
                "ground_truth_service": "front-end",
                "chaos_type": "cpu-hog",
            },
            {
                "cell_id": "no-exact-case",
                "run_status": "completed",
                "ground_truth_service": "orders",
                "chaos_type": "http-status-code",
            },
        ]
    }
    responses = {
        case_id: {
            "status": "inconclusive",
            "source_run_id": f"test-{case_id}",
            "a": a.model_dump(),
            "evidence": evidence,
        }
        for case_id in ("matched-case", "no-exact-case")
    }
    contract = {
        "known_services": ["front-end", "orders"],
        "possible_fault_types": ["cpu-hog", "http-status-code"],
    }

    cases, excluded = replay._prepare_cases(
        evaluation,
        responses,
        {"pairs": []},
        contract,
        a_text_version=rca_knowledge.LEGACY_A_TEXT_VERSION,
        retrieval_backend="opensearch",
        opensearch_index=index,
        include_unvalidated_experimental=True,
    )

    assert [case["case_id"] for case in cases] == [
        "matched-case",
        "no-exact-case",
    ]
    assert excluded == []
    assert all(case["oracle_pair_id"] is None for case in cases)
    assert all(case["history"] == [history] for case in cases)
    assert all(call["include_unvalidated_experimental"] for call in index.calls)
    assert all(call["top_k"] == 3 for call in index.calls)


def test_opensearch_report_scores_exact_recall_without_indexed_labels():
    case = {
        "case_id": "case",
        "labels": {"service": "front-end", "fault_type": "cpu-hog"},
        "retrieved": [
            {"pair_id": "exact", "effect_validated": False},
            {"pair_id": "neighbor", "effect_validated": True},
        ],
        "oracle_pair_id": None,
        "oracle_pair_effect_validated": None,
    }
    results = [
        {
            "case_id": "case",
            "condition": condition,
            "joint_correct": False,
            "service_correct": False,
            "fault_type_correct": False,
            "diagnosis": {
                "status": "inconclusive",
                "primary_service": "orders",
                "fault_type": "unknown",
            },
        }
        for condition in replay.CONDITIONS
    ]
    report = replay._summarize(
        results,
        [case],
        {
            "sha256": "base-sha",
            "pairs": [
                {
                    "pair_id": "exact",
                    "labels": {"service": "front-end", "fault_type": "cpu-hog"},
                    "effect_validated": False,
                },
                {
                    "pair_id": "neighbor",
                    "labels": {"service": "orders", "fault_type": "memory-hog"},
                    "effect_validated": True,
                },
            ],
        },
        "input-sha",
        "hybrid",
        [],
        retrieval_backend="opensearch",
        include_unvalidated_experimental=True,
        opensearch_metadata={"index_name": "test-index"},
    )

    assert report["retrieval_backend"] == "opensearch"
    assert report["retrieval_summary"]["cases_with_corresponding_pair"] == 1
    assert report["retrieval_summary"]["exact_pairs_retrieved_at_3"] == 1
    assert report["retrieval_summary"]["exact_pair_recall_at_3"] == 1.0
    assert report["retrieval_summary"]["unvalidated_pairs_in_top3"] == 1
    assert report["per_case"][0]["retrieval_exact_at_3"] is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1/chat/completions",
        "http://localhost:8001/v1/chat/completions?token=secret",
    ],
)
def test_model_requires_credential_free_local_endpoint(url):
    with pytest.raises(replay.campaign_knowledge.EvaluationError):
        replay.local_model(url, "qwen-14b", 1024, 100_000)


def test_local_model_disables_thinking_and_tools(monkeypatch):
    captured = {}

    def fake_call(messages, tools, **kwargs):
        captured.update(messages=messages, tools=tools, **kwargs)
        return {"content": "{}"}

    monkeypatch.setattr(replay, "call_llm_tool_messages", fake_call)
    call = replay.local_model(
        "http://127.0.0.1:8001/v1/chat/completions",
        "qwen3.5-9b-grafana",
        8192,
        100_000,
    )
    call([{"role": "user", "content": "{}"}], {})

    assert captured["tools"] == []
    assert captured["tool_choice"] == "none"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_v2_replay_rebuilds_training_index_from_frozen_source_evidence(
    tmp_path: Path, monkeypatch
):
    import numpy as np

    a, _ = frozen_inputs()
    evidence = [
        {
            "id": "E1",
            "tool": "get_run_golden_signals",
            "family": "metrics",
            "source": "live",
            "status": "ok",
            "observation_count": 6,
            "arguments": {
                "baseline_start": "2026-10-02T10:00:00Z",
                "baseline_end": "2026-10-02T10:01:00Z",
                "fault_start": "2026-10-02T10:02:00Z",
                "fault_end": "2026-10-02T10:03:00Z",
            },
            "digest": {
                "metrics": {
                    "cpu_saturation_pct": {
                        "status": "ok",
                        "unit": "%",
                        "phases": {
                            "baseline": {"status": "ok", "sample_count": 3},
                            "fault": {"status": "ok", "sample_count": 3},
                        },
                        "top_delta": [
                            {
                                "service": "front-end",
                                "baseline": 10.0,
                                "fault": 40.0,
                                "delta": 30.0,
                            }
                        ],
                    }
                }
            },
        }
    ]
    pair = {
        "pair_id": "pair",
        "source_run_id": "train-run",
        "source_cell_id": "train-cell",
        "a": a.model_dump(),
    }
    base = {
        "a_text_version": rca_knowledge.LEGACY_A_TEXT_VERSION,
        "embedding_provider": "test-provider",
        "embedding_dim": 2,
        "pairs": [pair],
        "index": {"pair": [0.0, 1.0]},
    }
    artifact = tmp_path / "rag_results/train-cell/annotate.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "response": {
                    "source_run_id": "train-run",
                    "initial_result": {"evidence": evidence},
                }
            }
        ),
        encoding="utf-8",
    )
    provider = type(
        "Provider",
        (),
        {
            "name": "test-provider",
            "dim": 2,
            "embed": staticmethod(
                lambda text: np.asarray(
                    [1.0, 0.0] if isinstance(text, str) else [0.0, 1.0],
                    dtype=np.float32,
                )
            ),
        },
    )()
    monkeypatch.setattr(replay, "get_embedding_provider", lambda: provider)

    versioned = replay._version_base(
        base,
        tmp_path,
        {"known_services": ["front-end"]},
        rca_knowledge.A_TEXT_VERSION,
    )

    assert base["a_text_version"] == rca_knowledge.LEGACY_A_TEXT_VERSION
    assert versioned["a_text_version"] == rca_knowledge.A_TEXT_VERSION
    assert versioned["pairs"][0]["a"]["metric_facts"][0]["service"] == "front-end"
    assert versioned["index"]["pair"] == [1.0, 0.0]
    assert versioned["sha256"] == rca_knowledge.digest(
        {key: value for key, value in versioned.items() if key != "sha256"}
    )
