import json

import pytest
import yaml

from experiment.eval.memory_hog.retroactive_b import (
    _generate_retro_b,
    _training_cohort,
    _validate_retro_b,
)
from experiment.platform.tests.test_rca_skill import diagnosis


def _campaign(tmp_path):
    train = [
        {
            "cell_id": f"train-{index}",
            "service": "orders",
            "chaos_type": "cpu-hog",
            "replica_idx": index,
            "phase": "train",
        }
        for index in range(35)
    ]
    test = [
        {
            "cell_id": f"test-{index}",
            "service": "orders",
            "chaos_type": "cpu-hog",
            "replica_idx": index,
            "phase": "test",
        }
        for index in range(35)
    ]
    config = {
        "experiment_campaign_id": "test-campaign",
        "knowledge_campaign": {"corpus_id": "test-corpus"},
        "train_experiments": train,
        "test_experiments": test,
    }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    cells = train + test
    (tmp_path / "plan.json").write_text(json.dumps(cells), encoding="utf-8")
    ledger = [
        {
            "cell_id": cell["cell_id"],
            "status": "completed" if index == 0 else "blocked",
            "run_id": "train-run-0" if index == 0 else None,
        }
        for index, cell in enumerate(train)
    ]
    (tmp_path / "ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in ledger),
        encoding="utf-8",
    )
    return config, cells


def _valid_guide():
    return {
        "target_result": {
            "service": "orders",
            "fault_type": "cpu-hog",
            "causal_category": "cpu-exhaustion",
        },
        "target_result_source": "ground_truth_label",
        "diagnosis": {
            "primary_service": "orders",
            "fault_type": "cpu-hog",
            "category": "cpu-exhaustion",
        },
        "evidence_assessment": [
            {
                "relation": "supports",
                "finding": "A telemetria disponível é compatível com a hipótese.",
                "evidence_ids": ["E1"],
            }
        ],
        "symptom_relation": "Verifique se a saturação antecede a latência.",
        "investigation_steps": [
            {
                "kind": "observed",
                "family": "metrics",
                "service": "orders",
                "phase": "fault",
                "objective": "Comparar CPU e latência.",
                "verification": "Use a série temporal do serviço.",
                "expected_support": "Saturação antes da regressão.",
                "expected_refutation": "CPU estável durante a regressão.",
                "evidence_ids": ["E1"],
            },
            {
                "kind": "recommended",
                "family": "traces",
                "service": "orders",
                "phase": "fault",
                "objective": "Localizar a operação afetada.",
                "verification": "Decompor a duração dos spans.",
                "expected_support": "A latência se origina em orders.",
                "expected_refutation": "A latência começa em uma dependência.",
                "evidence_ids": [],
            },
        ],
        "applicability": ["Revalidar o mecanismo no run atual."],
        "recommended_checks": ["Comparar spans atuais com o baseline."],
        "limitations": ["Os rótulos são ground truth do caso histórico."],
    }


def test_training_cohort_keeps_only_completed_training_run_ids(tmp_path):
    _, cells = _campaign(tmp_path)

    _, train_cells, cohort = _training_cohort(tmp_path)

    assert len(train_cells) == 35
    assert cohort["completed_cells"] == ["train-0"]
    assert cohort["latest"]["train-0"]["run_id"] == "train-run-0"
    assert all(cell["phase"] == "train" for cell in train_cells)
    assert len(cells) == 70


def test_training_cohort_rejects_any_started_test_run(tmp_path):
    _, cells = _campaign(tmp_path)
    test_cell = next(cell for cell in cells if cell["phase"] == "test")
    with (tmp_path / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "cell_id": test_cell["cell_id"],
            "status": "completed",
            "run_id": "test-run-0",
        }) + "\n")

    with pytest.raises(ValueError, match="forbidden after test runs start"):
        _training_cohort(tmp_path)


def test_retro_b_marks_target_as_ground_truth_and_cites_only_available_evidence():
    _validate_retro_b(
        _valid_guide(),
        expected={
            "service": "orders",
            "fault_type": "cpu-hog",
            "causal_category": "cpu-exhaustion",
        },
        known_services={"orders"},
        categories={"cpu-exhaustion"},
        evidence=[
            {
                "id": "E1",
                "status": "ok",
                "observation_count": 1,
                "fault_observation_count": 1,
            }
        ],
        possible_fault_types=["cpu-hog"],
    )


def test_retro_b_rejects_treating_ground_truth_as_observed_evidence():
    guide = _valid_guide()
    guide["investigation_steps"][1]["evidence_ids"] = ["E1"]

    with pytest.raises(ValueError, match="must not claim executed evidence"):
        _validate_retro_b(
            guide,
            expected={
                "service": "orders",
                "fault_type": "cpu-hog",
                "causal_category": "cpu-exhaustion",
            },
            known_services={"orders"},
            categories={"cpu-exhaustion"},
            evidence=[
                {
                    "id": "E1",
                    "status": "ok",
                    "observation_count": 1,
                    "fault_observation_count": 1,
                }
            ],
            possible_fault_types=["cpu-hog"],
        )


def test_retro_b_rejects_target_labels_that_differ_from_training_ground_truth():
    guide = _valid_guide()
    guide["target_result"]["service"] = "payment"

    with pytest.raises(ValueError, match="exactly reproduce"):
        _validate_retro_b(
            guide,
            expected={
                "service": "orders",
                "fault_type": "cpu-hog",
                "causal_category": "cpu-exhaustion",
            },
            known_services={"orders", "payment"},
            categories={"cpu-exhaustion"},
            evidence=[],
            possible_fault_types=["cpu-hog"],
        )


def test_focused_annotation_schema_pins_labels_but_does_not_force_confirmation():
    value = _valid_guide()
    value["diagnosis"] = diagnosis(
        primary_service="orders", category="cpu-exhaustion", fault_type="cpu-hog",
        supporting_evidence=["E1"], status="inconclusive",
    )
    schemas = []

    def model(messages, tools, **kwargs):
        schemas.append(kwargs["response_format"]["json_schema"]["schema"])
        return {"content": json.dumps(value)}

    guide, attempts, _ = _generate_retro_b(
        llm=model, labels=value["target_result"], document_a={},
        evidence=[{
            "id": "E1", "family": "metrics", "status": "ok",
            "observation_count": 1, "fault_observation_count": 1,
        }],
        original_diagnosis={}, executed_checks=[], known_services={"orders"},
        categories={"cpu-exhaustion"}, possible_fault_types=["cpu-hog"],
        focused_evidence=[], restarted_services=[], max_attempts=2,
    )
    definitions = schemas[0]["$defs"]
    assert definitions["TargetResult"]["properties"]["service"]["enum"] == ["orders"]
    assert definitions["Diagnosis"]["properties"]["category"]["enum"] == ["cpu-exhaustion"]
    assert definitions["Diagnosis"]["properties"]["restart_analysis"]["maxItems"] == 0
    assert guide["diagnosis"]["status"] == "inconclusive"
    assert len(attempts) == 1


def test_contract_repair_cannot_turn_uncertainty_into_confirmation():
    value = _valid_guide()
    value["diagnosis"] = diagnosis(
        primary_service="orders", category="cpu-exhaustion", fault_type="cpu-hog",
        supporting_evidence=["E1"], status="inconclusive",
    )
    value["investigation_steps"][1]["evidence_ids"] = ["E1"]
    calls = []

    def model(messages, tools, **kwargs):
        calls.append(len(messages))
        if len(calls) == 2:
            assert messages[-2]["role"] == "assistant"
            value["investigation_steps"][1]["evidence_ids"] = []
            value["diagnosis"]["status"] = "confirmed"
        return {"content": json.dumps(value)}

    with pytest.raises(ValueError, match="cannot strengthen"):
        _generate_retro_b(
            llm=model, labels=value["target_result"], document_a={},
            evidence=[{
                "id": "E1", "family": "metrics", "status": "ok",
                "observation_count": 1, "fault_observation_count": 1,
            }],
            original_diagnosis={}, executed_checks=[], known_services={"orders"},
            categories={"cpu-exhaustion"}, possible_fault_types=["cpu-hog"],
            focused_evidence=[], restarted_services=[], max_attempts=2,
        )
    assert len(calls) == 2
