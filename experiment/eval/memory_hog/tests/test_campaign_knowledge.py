from __future__ import annotations

import csv
import fcntl
import hashlib
import json
from collections import Counter

import pytest

from experiment.eval.memory_hog import campaign_knowledge as ck

SETTINGS = {"model": "fixture-only", "url": "no-network"}
WINDOWS = {
    "baseline": {"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T10:01:00Z"},
    "fault": {"start": "2026-10-01T10:02:00Z", "end": "2026-10-01T10:03:00Z"},
    "post": {"start": "2026-10-01T10:04:00Z", "end": "2026-10-01T10:05:00Z"},
}


def sources(service="front-end", fault="cpu-hog"):
    points = [
        [ck.timestamp(window["start"]), value]
        for window, value in zip(WINDOWS.values(), (1, 3, 1))
    ]
    return {
        "observation_window.json": {
            "phases": WINDOWS,
            "experiment_id": "PRIVATE_INTERVENTION_NAME",
            "phase_sources": {"fault": "observed"},
        },
        "metrics_raw.json": {
            "data": {
                "cpu_usage_cores": {
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "metric": {
                                    "service": service,
                                    "ground_truth_service": "PRIVATE_LABEL",
                                },
                                "values": points,
                            }
                        ]
                    },
                },
            },
        },
        "rca_evidence_snapshot.json": {
            "status": "ok",
            "services": {
                service: {
                    "phases": {
                        phase: {
                            "status": "ok",
                            "coverage": {"truncated": True},
                            "sample_lines": [
                                {
                                    "timestamp": window["start"],
                                    "container": service,
                                    "message": "CPU throttling observed",
                                }
                            ],
                        }
                        for phase, window in WINDOWS.items()
                    },
                },
            },
        },
        "traces_raw.json": {"count": 0, "collection": {"empty": True}},
        "controlled_report.json": {
            "service": service,
            "fault": fault,
            "gates": {"mechanism_observed": {"passed": True}},
            "private": "PRIVATE_INJECTOR",
        },
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / ".campaign.lock").touch()
    plan = [
        {"service": service, "chaos_type": fault, "replica_idx": 0}
        for service in ck.SERVICES
        for fault in ck.FAULTS
    ]
    ck.write_new(campaign / "plan.json", plan)
    store = tmp_path / "store"
    rows = []
    for index, cell in enumerate(plan):
        run_id = f"run-{index:03d}"
        rows.append({**cell, "phase": "test", "status": "completed", "run_id": run_id})
        for name, value in sources(cell["service"], cell["chaos_type"]).items():
            experiment_id = f"{campaign.name}-{cell['service']}-{cell['chaos_type']}-r0"
            if name == "observation_window.json":
                value["run_id"] = run_id
                value["experiment_id"] = experiment_id
            if name == "rca_evidence_snapshot.json":
                value["run_id"] = run_id
            ck.write_new(store / run_id / name, value)
        ck.write_new(
            store / run_id / "effective_spec.json",
            {
                "experiment": {"id": experiment_id},
                "observability": {
                    "metrics": {
                        "mcp_sse_url": "http://127.0.0.1:18080/sse",
                        "queries": [
                            {"id": "cpu_usage_cores", "query": "cpu_usage_cores"}
                        ],
                    },
                    "traces": {
                        "mcp_sse_url": "http://127.0.0.1:18080/sse",
                        "query": '{ resource.k8s.namespace.name = "sock-shop" }',
                        "limit": 2,
                    },
                },
            },
        )
    with (campaign / "runs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    output = tmp_path / "evaluation"
    ck.initialize(campaign, output, evidence_mode="live_per_condition")
    manifest = ck.load_sealed(output / "manifest.json")
    # The fixture uses its own object store, never the operational database.
    manifest.pop("sha256")
    manifest["object_store"] = str(store)
    (output / "manifest.json").write_text(ck.canonical(ck.seal(manifest)))
    real_collect = ck.collect_live_sources

    def collect_with_fake_mcp(
        manifest,
        bundle,
        config,
        client_factory=None,
        historical_observability=None,
    ):
        return real_collect(
            manifest,
            bundle,
            config,
            client_factory or (lambda **kwargs: FakeTelemetry([])),
            historical_observability,
        )

    monkeypatch.setattr(ck, "collect_live_sources", collect_with_fake_mcp)
    return campaign, output, store


def test_initialize_rejects_frozen_evidence_mode(tmp_path):
    with pytest.raises(ck.EvaluationError, match="offline evidence is prohibited"):
        ck.initialize(
            tmp_path / "campaign",
            tmp_path / "evaluation",
            evidence_mode="offline",
        )
    assert not (tmp_path / "evaluation").exists()


def fake_model(messages, schema):
    title = schema["title"]
    if title == "Observations":
        value = {
            "findings": [{"observation": "CPU increased", "evidence_ids": ["E0001"]}],
            "alternatives": ["Local CPU pressure", "Dependency wait"],
            "limitations": ["Limited sample"],
        }
    elif title == "Experience":
        value = {
            "mechanism_supported": True,
            "explanation": "Reviewed experimental mechanism",
            "discriminators": [
                {"observation": "CPU increase", "evidence_ids": ["E0001"]}
            ],
            "contradictions": [],
            "limitations": [],
        }
    else:
        value = {
            "status": "inconclusive",
            "primary_service": "front-end",
            "category": "cpu-exhaustion",
            "fault_type": "cpu-hog",
            "mechanism": "Resource pressure is a hypothesis",
            "supporting_evidence": ["E0001"],
            "contradicting_evidence": [],
            "alternatives": [
                {
                    "hypothesis": name,
                    "supporting_evidence": [],
                    "contradicting_evidence": [],
                }
                for name in ("CPU", "Memory")
            ],
            "causal_chain": [],
            "restart_analysis": [],
            "limitations": ["No causal confirmation"],
            "next_checks": [],
        }
    return {"content": json.dumps(value), "usage": {"total_tokens": 10}}


def reviewed(output):
    ck.freeze(output)
    ck.train(output, fake_model, settings=SETTINGS)
    manifest = ck.load_sealed(output / "manifest.json")
    decisions = {
        case["case_id"]: {
            "decision": "approve",
            "reviewer": "fixture-reviewer",
            "note": "Synthetic references checked",
        }
        for case in manifest["cases"]
        if case["split"] == "support"
        and (output / "drafts" / f"{case['case_id']}.json").is_file()
    }
    path = output / "review.json"
    ck.write_new(path, decisions)
    return ck.publish(output, path)


@pytest.fixture
def linked_workspace(workspace):
    parent, _, store = workspace
    child = parent.parent / "continuation"
    child.mkdir()
    (child / ".campaign.lock").touch()
    plan = ck.read_json(parent / "plan.json")
    for cell in plan:
        cell["cell_id"] = f"{cell['service']}-{cell['chaos_type']}-r0"
    (parent / "plan.json").write_text(ck.canonical(plan))
    config = {"experiment_campaign_id": parent.name, "test_experiments": plan}
    ck.write_new(parent / "config.yaml", config)
    with (parent / "runs.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))

    def save(directory, data):
        with (directory / "runs.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)
        (directory / "ledger.jsonl").write_text(
            "".join(
                ck.canonical(
                    {
                        "cell_id": f"{row['service']}-{row['chaos_type']}-r0",
                        "run_id": row["run_id"],
                        "status": row["status"],
                        "attempt": 1,
                    }
                )
                + "\n"
                for row in data
                if row["status"] != "not_run"
            )
        )

    child_rows = [dict(row) for row in rows[23:]]
    for index, row in enumerate(rows):
        if index >= 23:
            row["status"] = (
                ("blocked", "blocked", "aborted")[index - 23]
                if index < 26
                else "not_run"
            )
            if row["status"] == "not_run":
                row["run_id"] = ""
    save(parent, rows)
    ck.write_new(child / "plan.json", plan[23:])
    ck.write_new(
        child / "config.yaml",
        {
            "experiment_campaign_id": child.name,
            "test_experiments": plan[23:],
            "continuation": {
                "parent_campaign_id": parent.name,
                "parent_directory": str(parent),
                "source_sha256": {
                    name: hashlib.sha256((parent / name).read_bytes()).hexdigest()
                    for name in ("config.yaml", "plan.json", "ledger.jsonl")
                },
            },
        },
    )
    for index, row in enumerate(child_rows):
        if index >= 9:
            row["status"] = ("blocked", "blocked", "aborted")[index - 9]
        for name in ("observation_window.json", "effective_spec.json"):
            path = store / row["run_id"] / name
            data = ck.read_json(path)
            experiment_id = f"{child.name}-{row['service']}-{row['chaos_type']}-r0"
            if name == "observation_window.json":
                data["experiment_id"] = experiment_id
            else:
                data["experiment"]["id"] = experiment_id
            path.write_text(ck.canonical(data))
    save(child, child_rows)
    output = parent.parent / "linked-evaluation"
    ck.initialize(
        parent,
        output,
        continuation=child,
        evidence_mode="live_per_condition",
    )
    manifest = ck.load_sealed(output / "manifest.json")
    manifest.pop("sha256")
    manifest["object_store"] = str(store)
    (output / "manifest.json").write_text(ck.canonical(ck.seal(manifest)))
    return parent, child, output, store


def test_linked_32_completed_selection_and_end_to_end(linked_workspace):
    parent, child, output, _ = linked_workspace
    originals = {
        path: path.read_bytes()
        for directory in (parent, child)
        for path in directory.iterdir()
        if path.is_file()
    }
    knowledge = reviewed(output)
    snapshot = ck.load_sealed(output / "snapshot.json")
    manifest = ck.load_manifest(output)
    assert len(snapshot["bundles"]) == 32
    assert len(snapshot["exclusions"]) == 3
    assert Counter(snapshot["case_sources"].values()) == {
        parent.name: 23,
        child.name: 9,
    }
    assert manifest["cases"] == ck.partition(
        ck.read_json(parent / "plan.json"), manifest["seed"]
    )
    assert (
        len(
            {
                item["run_id"]
                for item in ck.load_sealed(output / "private/labels.json")[
                    "provenance"
                ].values()
            }
        )
        == 32
    )
    support = {
        case["case_id"] for case in manifest["cases"] if case["split"] == "support"
    }
    assert {item["support_case_id"] for item in knowledge["items"]} <= support
    report = ck.evaluate(output, fake_model, 1, settings=SETTINGS)
    assert report["dataset"]["included_completed"] == 32
    assert report["test_cases"] + report["dataset"]["support_cases"] == 32
    assert set(report["summary_by_segment"]) == {parent.name, child.name}
    for condition in ck.CONDITIONS:
        assert (
            sum(
                segment[condition]["cases"]
                for segment in report["summary_by_segment"].values()
            )
            == report["summary"][condition]["cases"]
        )
    for attempt in (output / "attempts").glob("*.json"):
        result = ck.load_sealed(attempt)
        assert result["case_id"] not in support
        assert "source_campaign_id" not in ck.canonical(result["messages"])
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize("source_index", [0, 1])
def test_linked_lock_blocks_all_evaluation_work(linked_workspace, source_index):
    parent, child, output, _ = linked_workspace
    with ((parent, child)[source_index] / ".campaign.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ck.EvaluationError, match="running"):
            ck.freeze(output)
        with pytest.raises(ck.EvaluationError, match="running"):
            ck.train(output, fake_model, settings=SETTINGS)
    assert not (output / "snapshot.json").exists()


@pytest.mark.parametrize(
    "file_name", ["config.yaml", "plan.json", "ledger.jsonl", "runs.csv"]
)
def test_linked_source_mutation_rejected(linked_workspace, file_name):
    _, child, output, _ = linked_workspace
    path = child / file_name
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ck.EvaluationError, match="changed after initialization"):
        ck.freeze(output)


def test_linked_wrong_parent_and_duplicate_selection_rejected(linked_workspace):
    parent, child, _, _ = linked_workspace
    config = ck.read_json(child / "config.yaml")
    config["continuation"]["parent_campaign_id"] = "foreign"
    (child / "config.yaml").write_text(ck.canonical(config))
    with pytest.raises(ck.EvaluationError, match="parent workflow"):
        ck.initialize(parent, child.parent / "foreign-eval", continuation=child)
    config["continuation"]["parent_campaign_id"] = parent.name
    config["test_experiments"] = ck.read_json(parent / "plan.json")
    (child / "config.yaml").write_text(ck.canonical(config))
    (child / "plan.json").write_text(ck.canonical(config["test_experiments"]))
    with pytest.raises(ck.EvaluationError, match="pending parent cells"):
        ck.initialize(parent, child.parent / "duplicate-eval", continuation=child)


def test_linked_unresolved_csv_or_ledger_rejected(linked_workspace):
    parent, child, _, _ = linked_workspace
    path = child / "ledger.jsonl"
    path.write_text(path.read_text().replace('"completed"', '"submitted"', 1))
    with pytest.raises(ck.EvaluationError, match="unresolved"):
        ck.initialize(parent, child.parent / "active-eval", continuation=child)
    path.write_text(path.read_text().replace('"submitted"', '"completed"', 1))
    path = child / "runs.csv"
    path.write_text(path.read_text().replace("completed", "aborted", 1))
    with pytest.raises(ck.EvaluationError, match="CSV disagrees"):
        ck.initialize(parent, child.parent / "csv-eval", continuation=child)


@pytest.mark.parametrize(
    "ledger_run,ledger_attempt,accepted",
    [
        ("run-000", 2, True),
        ("run-other", 2, False),
        ("run-000", 3, False),
    ],
)
def test_retry_identity_requires_exact_ledger_link(
    workspace, ledger_run, ledger_attempt, accepted
):
    campaign, output, store = workspace
    root = store / "run-000"
    window = ck.read_json(root / "observation_window.json")
    spec = ck.read_json(root / "effective_spec.json")
    experiment_id = window["experiment_id"] + "-attempt-2"
    window["experiment_id"] = experiment_id
    spec["experiment"]["id"] = experiment_id
    (root / "observation_window.json").write_text(ck.canonical(window))
    (root / "effective_spec.json").write_text(ck.canonical(spec))
    (campaign / "ledger.jsonl").write_text(
        ck.canonical(
            {
                "cell_id": "front-end-cpu-hog-r0",
                "run_id": ledger_run,
                "attempt": ledger_attempt,
                "status": "completed",
            }
        )
        + "\n"
    )
    if accepted:
        assert ck.freeze(output)["campaign_ledger_sha256"]
    else:
        with pytest.raises(ck.EvaluationError, match="identity"):
            ck.freeze(output)


def test_partition_is_balanced_reproducible_and_not_a_fault_holdout():
    plan = [{"service": s, "chaos_type": f} for s in ck.SERVICES for f in ck.FAULTS]
    cases = ck.partition(plan, 42)
    assert cases == ck.partition(plan, 42)
    assert cases != ck.partition(plan, 43)
    assert Counter(case["split"] for case in cases) == {"support": 17, "test": 18}
    for split in ("support", "test"):
        subset = [case for case in cases if case["split"] == split]
        assert {case["service"] for case in subset} == set(ck.SERVICES)
        assert {case["chaos_type"] for case in subset} == set(ck.FAULTS)
    with pytest.raises(ck.EvaluationError, match="35 medium"):
        ck.partition(plan[:-1], 42)


def test_init_is_safe_during_campaign_and_never_resets_data(workspace):
    campaign, output, _ = workspace
    with (campaign / ".campaign.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        other = output.parent / "other"
        ck.initialize(campaign, other)
        assert (other / "manifest.json").is_file()
    with pytest.raises(ck.EvaluationError, match="never reset"):
        ck.initialize(campaign, output)


def test_campaign_lock_blocks_freeze_and_model_work(workspace):
    campaign, output, _ = workspace
    with (campaign / ".campaign.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ck.EvaluationError, match="running"):
            ck.freeze(output)
        with pytest.raises(ck.EvaluationError, match="running"):
            ck.train(output, fake_model, settings=SETTINGS)
    assert not (output / "snapshot.json").exists()
    assert not (output / "model_settings.json").exists()


def test_terminal_ledger_required_even_without_process_lock(workspace):
    campaign, output, _ = workspace
    path = campaign / "runs.csv"
    path.write_text(path.read_text().replace("completed", "not_run", 1))
    with pytest.raises(ck.EvaluationError, match="unfinished"):
        ck.freeze(output)


def test_snapshot_strips_labels_and_keeps_same_evidence_for_conditions(workspace):
    _, output, _ = workspace
    snapshot = ck.freeze(output)
    text = ck.canonical(snapshot)
    assert "PRIVATE_" not in text
    assert "controlled_report" not in text
    assert "ground_truth_service" not in text
    manifest = ck.load_sealed(output / "manifest.json")
    bundle = next(iter(snapshot["bundles"].values()))
    a = json.loads(ck.build_messages(manifest, bundle, "skill", [])[1]["content"])
    b = json.loads(ck.build_messages(manifest, bundle, "skill_card", [])[1]["content"])
    assert a["incident"] == b["incident"]
    assert a["contract"] == b["contract"]
    assert "application_context" not in a
    assert "application_context" in b
    assert "past_experiences" not in b


def test_missing_traces_and_search_error_are_not_absence_of_failure():
    data = sources()
    data["traces_raw.json"]["collection"]["search_error"] = "timeout"
    assert ck.make_bundle("case-x", data)["evidence"][-1]["status"] == "tool_error"
    data.pop("traces_raw.json")
    assert (
        ck.make_bundle("case-x", data)["evidence"][-1]["status"] == "missing_artifact"
    )
    data["traces_raw.json"] = {"count": 1, "traces": [{"private": "not forwarded"}]}
    trace = ck.make_bundle("case-x", data)["evidence"][-1]
    assert trace["status"] == "unsupported_schema"
    assert "not forwarded" not in ck.canonical(trace)


def test_log_window_and_injector_filter():
    data = sources()
    logs = data["rca_evidence_snapshot.json"]["services"]["front-end"]["phases"][
        "fault"
    ]
    logs["sample_lines"].extend(
        [
            {"timestamp": WINDOWS["baseline"]["start"], "message": "old"},
            {
                "timestamp": WINDOWS["fault"]["start"],
                "container": "go-runner",
                "message": "INJECTOR_LABEL",
            },
        ]
    )
    bundle = ck.make_bundle("case-x", data)
    fault_logs = next(
        item for item in bundle["evidence"] if item.get("phase") == "fault"
    )
    assert fault_logs["rejected_lines"] == 2
    assert "INJECTOR_LABEL" not in ck.canonical(bundle)


def test_training_is_blind_before_label_and_resumable(workspace):
    _, output, _ = workspace
    ck.freeze(output)
    calls = []

    def model(messages, schema):
        calls.append((messages, schema["title"]))
        return fake_model(messages, schema)

    ck.train(output, model, settings=SETTINGS)
    assert len(calls) == 34
    for messages, title in calls:
        text = ck.canonical(messages)
        if title == "Observations":
            assert "experimental_label" not in text
        else:
            assert "experimental_label" in text
    assert len(list((output / "drafts").glob("*.json"))) == 17
    ck.train(output, model, settings=SETTINGS)
    assert len(calls) == 34
    with pytest.raises(ck.EvaluationError, match="settings changed"):
        ck.train(output, model, settings={"model": "different"})


def test_invalid_training_refs_are_audited_not_published(workspace):
    _, output, _ = workspace
    ck.freeze(output)

    def wrong(messages, schema):
        result = fake_model(messages, schema)
        result["content"] = result["content"].replace("E0001", "E9999")
        return result

    report = ck.train(output, wrong, settings=SETTINGS)
    assert report["errors"] == 17
    assert report["drafts"] == 0
    assert all("E9999" in row["error"] for row in report["results"])
    assert (output / "review_template.json").is_file()
    assert list((output / "private/model_calls").glob("*.json"))
    assert not (output / "knowledge.json").exists()


def test_test_case_cannot_be_published(workspace):
    _, output, _ = workspace
    ck.freeze(output)
    manifest = ck.load_sealed(output / "manifest.json")
    test = next(case for case in manifest["cases"] if case["split"] == "test")
    path = output / "decisions.json"
    ck.write_new(
        path, {test["case_id"]: {"decision": "approve", "reviewer": "x", "note": "x"}}
    )
    with pytest.raises(ck.EvaluationError, match="test/foreign"):
        ck.publish(output, path)


def test_unvalidated_mechanism_cannot_be_approved(workspace):
    _, output, store = workspace
    for path in store.glob("*/controlled_report.json"):
        report = ck.read_json(path)
        report["gates"]["mechanism_observed"]["passed"] = False
        path.write_text(ck.canonical(report))
    ck.freeze(output)
    ck.train(output, fake_model, settings=SETTINGS)
    first = next((output / "drafts").glob("*.json")).stem
    decisions = output / "decisions.json"
    ck.write_new(
        decisions, {first: {"decision": "approve", "reviewer": "x", "note": "x"}}
    )
    with pytest.raises(ck.EvaluationError, match="without experimental"):
        ck.publish(output, decisions)


def test_qualified_history_preserves_gate_and_model_status(workspace):
    _, output, store = workspace
    manifest = ck.load_manifest(output)
    manifest.pop("sha256")
    manifest["history_policy"] = "qualified_examples"
    (output / "manifest.json").write_text(ck.canonical(ck.seal(manifest)))
    for report_path in store.glob("*/controlled_report.json"):
        report = ck.read_json(report_path)
        report["gates"]["mechanism_observed"]["passed"] = False
        report_path.write_text(ck.canonical(report))
    ck.freeze(output)

    def unsupported_model(messages, schema):
        response = fake_model(messages, schema)
        if schema["title"] == "Experience":
            value = json.loads(response["content"])
            value["mechanism_supported"] = False
            value["discriminators"] = []
            value["contradictions"] = []
            response["content"] = json.dumps(value)
        return response

    ck.train(output, unsupported_model, settings=SETTINGS)
    decisions = {
        case["case_id"]: {
            "decision": "approve",
            "reviewer": "fixture-reviewer",
            "note": "Include as qualified negative example",
        }
        for case in ck.load_manifest(output)["cases"]
        if case["split"] == "support"
    }
    path = output / "decisions.json"
    ck.write_new(path, decisions)
    knowledge = ck.publish(output, path)
    assert len(knowledge["items"]) == 17
    assert all(
        item["qualification"]["classification"]
        == "gate_failed_model_not_supported"
        and item["qualification"]["causal_confirmation"] is False
        and "known_label" not in item
        and item["support_evidence"]
        for item in knowledge["items"]
    )


def test_qualified_history_classifies_all_gate_model_combinations():
    evidence = [{"id": "E0001", "family": "metrics"}]
    bundle = {"evidence": evidence}
    classifications = {
        (True, True): "gate_passed_model_supported",
        (True, False): "gate_passed_model_not_supported",
        (False, True): "gate_failed_model_supported",
        (False, False): "gate_failed_model_not_supported",
    }
    for (gate_passed, model_supported), expected in classifications.items():
        draft = {
            "case_id": "case-001",
            "label": {
                "service": "orders",
                "fault_type": "cpu-hog",
                "mechanism_validated": gate_passed,
            },
            "observations": {
                "findings": [
                    {"observation": "CPU changed", "evidence_ids": ["E0001"]}
                ],
                "alternatives": [],
                "limitations": [],
            },
            "experience": {
                "mechanism_supported": model_supported,
                "explanation": "Qualified observation",
                "discriminators": [],
                "contradictions": [],
                "limitations": [],
            },
        }
        item = ck.historical_item(
            draft, bundle, {"decision": "approve"}, "qualified_examples"
        )
        assert item["qualification"]["classification"] == expected


def test_publish_retrieve_and_paired_evaluation_end_to_end(workspace):
    _, output, _ = workspace
    knowledge = reviewed(output)
    assert len(knowledge["items"]) == 17
    manifest = ck.load_sealed(output / "manifest.json")
    support_ids = {
        case["case_id"] for case in manifest["cases"] if case["split"] == "support"
    }
    assert {item["support_case_id"] for item in knowledge["items"]} == support_ids
    for item in knowledge["items"]:
        assert all(
            evidence["id"].startswith(item["knowledge_id"] + ":")
            for evidence in item["support_evidence"]
        )
        assert all(
            ref.startswith(item["knowledge_id"] + ":")
            for finding in item["experience"]["discriminators"]
            for ref in finding["evidence_ids"]
        )
    seen = []

    def model(messages, schema):
        seen.append(messages)
        return fake_model(messages, schema)

    report = ck.evaluate(output, model, 2, settings=SETTINGS)
    assert len(seen) == 108
    for condition in ck.CONDITIONS:
        summary = report["summary"][condition]
        assert summary["cases"] == 18
        assert summary["attempts"] == 36
        assert summary["confirmation_label_precision"] is None
        assert summary["errors"] == 0
    for messages in seen:
        payload = json.loads(messages[1]["content"])
        assert "experimental_label" not in payload
        for item in payload.get("past_experiences", []):
            assert item["support_case_id"] in support_ids
    resumed = ck.evaluate(output, model, 2, settings=SETTINGS)
    assert resumed["summary"] == report["summary"]
    assert len(seen) == 108


def test_foreign_workflow_knowledge_is_rejected(workspace):
    _, output, _ = workspace
    knowledge = reviewed(output)
    knowledge.pop("sha256")
    knowledge["campaign_id"] = "different-campaign"
    (output / "knowledge.json").write_text(ck.canonical(ck.seal(knowledge)))
    with pytest.raises(ck.EvaluationError, match="another workflow"):
        ck.evaluate(output, fake_model, 1, settings=SETTINGS)


def test_model_errors_are_preserved_and_count_as_incorrect(workspace):
    _, output, _ = workspace
    reviewed(output)

    def broken(messages, schema):
        return {"content": "not JSON", "usage": {"total_tokens": 11}}

    report = ck.evaluate(output, broken, 1, settings=SETTINGS)
    for condition in ck.CONDITIONS:
        assert report["summary"][condition]["errors"] == 18
        assert report["summary"][condition]["accuracy"]["joint_correct"] == 0
        assert report["summary"][condition]["reported_total_tokens"] == 198
    assert len(list((output / "attempts").glob("*.json"))) == 54


def test_invalid_historical_citations_cannot_confirm_current_cause(workspace):
    _, output, _ = workspace
    reviewed(output)

    def historical_citation(messages, schema):
        result = fake_model(messages, schema)
        value = json.loads(result["content"])
        value["status"] = "confirmed"
        value["supporting_evidence"] = ["K-case-001"]
        value["causal_chain"] = ["Unverified historical analogy"]
        result["content"] = json.dumps(value)
        return result

    report = ck.evaluate(output, historical_citation, 1, settings=SETTINGS)
    for condition in ck.CONDITIONS:
        assert report["summary"][condition]["confirmation_count"] == 0


def test_checksum_tampering_fails(workspace):
    _, output, _ = workspace
    path = output / "manifest.json"
    manifest = ck.read_json(path)
    manifest["seed"] = 0
    path.write_text(ck.canonical(manifest))
    with pytest.raises(ck.EvaluationError, match="Integrity"):
        ck.freeze(output)


def test_only_local_inference_and_explicit_opt_in(tmp_path):
    with pytest.raises(ck.EvaluationError, match="local model"):
        ck.local_model("https://example.com/v1/chat/completions", "model", 100, 100)
    assert ck.main(["evaluate", "--output-dir", str(tmp_path)]) == 1
    model = ck.local_model("http://127.0.0.1:8001/v1/chat/completions", "model", 100, 1)
    with pytest.raises(ck.EvaluationError, match="budget exceeded"):
        model([{"role": "user", "content": "no request should occur"}], {})


def test_failed_cells_are_excluded_without_changing_partition(workspace):
    campaign, output, _ = workspace
    path = campaign / "runs.csv"
    path.write_text(path.read_text().replace("completed", "blocked", 1))
    snapshot = ck.freeze(output)
    assert len(snapshot["bundles"]) == 34
    assert len(snapshot["exclusions"]) == 1
    assert len(ck.load_sealed(output / "manifest.json")["cases"]) == 35


def test_trace_phase_attribution_excludes_generated_summaries_and_crossing_traces():
    trace = {
        "trace_id": "trace-1",
        "root_service": "orders",
        "start_time_unix_nano": str(int(ck.timestamp(WINDOWS["fault"]["start"]) * 1e9)),
        "duration_ms": 50,
        "features": {
            "span_count": 2,
            "critical_path_raw": [
                {"service": "orders", "name": "/checkout", "duration_ms": 50}
            ],
            "rca_hypotheses": ["PRIVATE_GENERATED_VERDICT"],
        },
        "summary": "PRIVATE_GENERATED_VERDICT",
    }
    result = ck.trace_evidence({"traces": [trace]}, WINDOWS)[0]
    assert result["phase"] == "fault"
    assert result["fault_observation_count"] == 2
    assert "PRIVATE_" not in ck.canonical(result)
    trace["duration_ms"] = 120000
    crossing = ck.trace_evidence({"traces": [trace]}, WINDOWS)[0]
    assert crossing["status"] == "outside_or_crosses_phase"
    assert crossing["fault_observation_count"] == 0


@pytest.mark.parametrize(
    "metric,baseline,fault,expected",
    [
        ("pod_restarts", 0, 1, ["orders"]),
        ("pod_restarts_total", 2, 3, ["orders"]),
        ("pod_restarts_total", 2, 2, []),
    ],
)
def test_restart_gate_uses_baseline_and_pod_identity(metric, baseline, fault, expected):
    bundle = {
        "evidence": [
            {
                "metric": metric,
                "labels": {"pod": "orders-abcdef-123"},
                "phases": {
                    "baseline": {"last": baseline},
                    "fault": {"last": fault, "max": fault},
                },
            }
        ]
    }
    assert ck.restarted_services(bundle, {"orders", "orders-db"}) == expected


def test_training_failure_resumes_from_blind_observations(workspace):
    _, output, _ = workspace
    ck.freeze(output)
    calls = []

    def fail_labeled(messages, schema):
        calls.append(schema["title"])
        if schema["title"] == "Experience":
            raise KeyboardInterrupt("Synthetic operator interruption")
        return fake_model(messages, schema)

    with pytest.raises(KeyboardInterrupt, match="Synthetic"):
        ck.train(output, fail_labeled, settings=SETTINGS)
    assert calls == ["Observations", "Experience"]
    assert len(list((output / "observations").glob("*.json"))) == 1
    records = [
        ck.load_sealed(path) for path in (output / "private/model_calls").glob("*.json")
    ]
    assert any("response" not in record for record in records)
    calls.clear()

    def resume(messages, schema):
        calls.append(schema["title"])
        return fake_model(messages, schema)

    ck.train(output, resume, settings=SETTINGS)
    assert calls[0] == "Experience"
    assert len(calls) == 33


def test_mismatched_private_label_snapshot_is_rejected(workspace):
    _, output, _ = workspace
    ck.freeze(output)
    path = output / "private/labels.json"
    private = ck.load_sealed(path)
    private.pop("sha256")
    private["snapshot_sha256"] = "different-snapshot"
    path.write_text(ck.canonical(ck.seal(private)))
    with pytest.raises(ck.EvaluationError, match="Private labels"):
        ck.train(output, fake_model, settings=SETTINGS)


def test_missing_logs_have_explicit_service_and_phase_coverage(workspace):
    _, output, store = workspace
    for path in store.glob("*/rca_evidence_snapshot.json"):
        path.unlink()
    snapshot = ck.freeze(output)
    known = ck.load_manifest(output)["contract"]["known_services"]
    for bundle in snapshot["bundles"].values():
        logs = [item for item in bundle["evidence"] if item["family"] == "logs"]
        assert len(logs) == len(known) * len(WINDOWS)
        assert {item["status"] for item in logs} == {"missing_artifact"}
        assert all(item["fault_observation_count"] == 0 for item in logs)


def test_missing_log_service_or_phase_is_not_no_data():
    raw = sources()["rca_evidence_snapshot.json"]
    raw["services"]["front-end"]["phases"].pop("fault")
    logs = ck.log_evidence(raw, WINDOWS, ["front-end", "orders"])
    assert len(logs) == 6
    assert all(
        item["status"] == "missing_coverage"
        for item in logs
        if item["service"] == "orders" or item["phase"] == "fault"
    )
    errors = ck.log_evidence({"status": "tool_error"}, WINDOWS, ["orders"])
    assert {item["status"] for item in errors} == {"tool_error"}


@pytest.mark.parametrize(
    "artifact,field,replacement",
    [
        ("observation_window.json", "run_id", "run-other"),
        ("observation_window.json", "experiment_id", "foreign-orders-cpu-hog-r0"),
        ("effective_spec.json", "experiment.id", "foreign-orders-cpu-hog-r0"),
        ("rca_evidence_snapshot.json", "run_id", "run-other"),
    ],
)
def test_freeze_rejects_cross_run_or_cross_campaign_artifacts(
    workspace, artifact, field, replacement
):
    _, output, store = workspace
    path = store / "run-000" / artifact
    value = ck.read_json(path)
    if field == "experiment.id":
        value["experiment"]["id"] = replacement
    else:
        value[field] = replacement
    path.write_text(ck.canonical(value))
    with pytest.raises(ck.EvaluationError, match="identity|run_id"):
        ck.freeze(output)
    assert not (output / "snapshot.json").exists()


def live_mode(output):
    path = output / "manifest.json"
    manifest = ck.load_manifest(output)
    manifest.pop("sha256")
    manifest["evidence_mode"] = "live_per_condition"
    manifest["design"] = "online-exploratory-historical-windows-v1"
    path.write_text(ck.canonical(ck.seal(manifest)))


class FakeTelemetry:
    def __init__(self, calls, *, fail=False):
        self.calls = calls
        self.fail = fail

    def call_tool_json(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fail:
            raise RuntimeError("Synthetic backend outage")
        if name == "prometheus_range_query":
            return sources()["metrics_raw.json"]["data"]["cpu_usage_cores"]
        if name == "capture_run_log_snapshot":
            return sources()["rca_evidence_snapshot.json"]
        if name == "tempo_search_traces":
            return {"traces": []}
        raise AssertionError(f"Unexpected tool {name}")


@pytest.mark.parametrize("configured_limit", [2, 50])
def test_live_collector_queries_historical_windows_and_records_calls(
    workspace, configured_limit
):
    _, output, _ = workspace
    manifest = ck.load_manifest(output)
    bundle = ck.make_bundle("case-x", sources())
    config = ck.read_json(next((workspace[2]).glob("*/effective_spec.json")))[
        "observability"
    ]
    config["traces"]["limit"] = configured_limit
    calls = []
    endpoints = []

    def client_factory(**kwargs):
        endpoints.append((kwargs["sse_url"], kwargs["timeout_seconds"]))
        return FakeTelemetry(calls)

    collected = ck.collect_live_sources(
        manifest,
        bundle,
        config,
        client_factory,
    )
    assert len(calls) == 5
    assert endpoints == [("http://127.0.0.1:18082/sse", 90)] * 5
    assert all(
        value is not None for _, arguments in calls for value in arguments.values()
    )
    assert calls[0][1]["start"] == WINDOWS["baseline"]["start"]
    assert calls[0][1]["end"] == WINDOWS["post"]["end"]
    assert [
        arguments["start"] for tool, arguments in calls if tool == "tempo_search_traces"
    ] == [window["start"] for window in WINDOWS.values()]
    assert collected["live_collection"]["fallback_to_stored_telemetry"] is False
    assert manifest["trace_limit_per_phase"] == 5
    assert collected["live_collection"]["trace_limit_per_phase"] == 5
    assert [
        arguments["limit"] for tool, arguments in calls if tool == "tempo_search_traces"
    ] == [min(configured_limit, 5)] * 3
    assert len(collected["live_collection"]["calls"]) == 5
    assert "controlled_report.json" not in collected
    assert "effective_spec.json" not in collected


def test_live_collector_prefers_configured_historical_mcp_over_live_urls(workspace):
    _, output, store = workspace
    manifest = ck.load_manifest(output)
    config = ck.read_json(next(store.glob("*/effective_spec.json")))["observability"]
    calls = []
    endpoints = []
    collected = ck.collect_live_sources(
        manifest,
        ck.make_bundle("case-x", sources()),
        config,
        lambda **kwargs: (
            endpoints.append((kwargs["sse_url"], kwargs["timeout_seconds"]))
            or FakeTelemetry(calls)
        ),
        historical_observability={
            "mcp_sse_url": "http://127.0.0.1:18083/sse",
            "timeout_seconds": 45,
        },
    )

    assert endpoints == [("http://127.0.0.1:18083/sse", 45)] * 5
    assert len(collected["live_collection"]["calls"]) == 5


@pytest.mark.parametrize("limit", [0, 51])
def test_initialization_rejects_invalid_trace_budget(workspace, limit):
    campaign, output, _ = workspace
    target = output.parent / "invalid-trace-budget"
    with pytest.raises(ck.EvaluationError, match="Trace query limit"):
        ck.initialize(campaign, target, trace_limit=limit)
    assert not target.exists()


def test_live_failures_never_fall_back_to_stored_logs_or_metrics(workspace):
    _, output, store = workspace
    manifest = ck.load_manifest(output)
    config = ck.read_json(next(store.glob("*/effective_spec.json")))["observability"]
    calls = []
    collected = ck.collect_live_sources(
        manifest,
        ck.make_bundle("case-x", sources()),
        config,
        lambda **kwargs: FakeTelemetry(calls, fail=True),
    )
    bundle = ck.make_bundle("case-x", collected, ["front-end"])
    assert len(calls) == 5
    assert {item["status"] for item in bundle["evidence"]} == {"tool_error"}
    assert all(item["observation_count"] == 0 for item in bundle["evidence"])


def test_live_train_and_each_condition_use_separate_persisted_collections(
    workspace, monkeypatch
):
    _, output, _ = workspace
    live_mode(output)
    real_collect = ck.collect_live_sources
    collections = []

    def collect(
        manifest, bundle, config, client_factory=None, historical_observability=None
    ):
        collections.append(bundle["case_id"])
        return real_collect(
            manifest,
            bundle,
            config,
            lambda **kwargs: FakeTelemetry([]),
            historical_observability,
        )

    monkeypatch.setattr(ck, "collect_live_sources", collect)
    knowledge = reviewed(output)
    assert len(collections) == 17
    assert all(item["retrieval_signature"] for item in knowledge["items"])
    report = ck.evaluate(output, fake_model, 1, settings=SETTINGS)
    assert len(collections) == 17 + 54
    assert report["evidence_mode"] == "live_per_condition"
    assert report["identical_evidence_across_conditions"] is False
    assert report["collection_count"] == 54
    records = list((output / "private/live_evidence").glob("*.json"))
    assert len(records) == 71
    for path in (output / "attempts").glob("*.json"):
        attempt = ck.load_sealed(path)
        record = ck.load_sealed(output / "private/live_evidence" / path.name)
        assert attempt["bundle_sha256"] == ck.digest(record["bundle"])
    ck.evaluate(output, fake_model, 1, settings=SETTINGS)
    ck.train(output, fake_model, settings=SETTINGS)
    assert len(collections) == 71


def test_live_mode_still_blocks_on_campaign_and_requires_cli_opt_in(
    workspace, monkeypatch
):
    campaign, output, _ = workspace
    live_mode(output)
    ck.freeze(output)

    def prohibited(*args, **kwargs):
        raise AssertionError("No backend call allowed while campaign is active")

    monkeypatch.setattr(ck, "collect_live_sources", prohibited)
    with (campaign / ".campaign.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(ck.EvaluationError, match="running"):
            ck.train(output, fake_model, settings=SETTINGS)
    assert (
        ck.main(
            [
                "train",
                "--output-dir",
                str(output),
                "--allow-inference",
                "--model",
                "fixture",
            ]
        )
        == 1
    )


def shared_mode(output, *, repairs=1):
    path = output / "manifest.json"
    manifest = ck.load_manifest(output)
    manifest.pop("sha256")
    manifest["evidence_mode"] = "live_shared"
    manifest["design"] = "paired-shared-historical-evidence-v1"
    manifest["max_repairs"] = repairs
    path.write_text(ck.canonical(ck.seal(manifest)))


def shared_collector(monkeypatch, calls, *, fail=False):
    real_collect = ck.collect_live_sources

    def collect(
        manifest, bundle, config, client_factory=None, historical_observability=None
    ):
        calls.append(bundle["case_id"])
        return real_collect(
            manifest,
            bundle,
            config,
            lambda **kwargs: FakeTelemetry([], fail=fail),
            historical_observability,
        )

    monkeypatch.setattr(ck, "collect_live_sources", collect)
    return collect


def test_shared_32_collected_once_and_identical_for_all_conditions(
    linked_workspace, monkeypatch
):
    _, _, output, _ = linked_workspace
    shared_mode(output)
    ck.freeze(output)
    collections = []
    shared_collector(monkeypatch, collections)
    index = ck.collect_shared(output)
    assert len(collections) == 32
    assert ck.collect_shared(output) == index
    report = ck.train(output, fake_model, settings=SETTINGS)
    assert report["support_cases"] == 16
    assert report["drafts"] == 16
    decisions = ck.read_json(output / "review_template.json")
    assert len(decisions) == 16
    assert all(row["decision"] == "pending" for row in decisions.values())
    assert not (output / "knowledge.json").exists()
    for row in decisions.values():
        row.update(decision="approve", reviewer="fixture", note="Synthetic review")
    path = output / "review.json"
    ck.write_new(path, decisions)
    ck.publish(output, path)
    evaluated = ck.evaluate(output, fake_model, 2, settings=SETTINGS)
    assert evaluated["test_cases"] == 16
    assert evaluated["collection_count"] == 32
    assert evaluated["identical_evidence_across_conditions"] is True
    assert evaluated["shared_evidence_sha256"] == index["sha256"]
    assert all(
        len(hashes) == 1 for hashes in evaluated["test_evidence_sha256"].values()
    )
    assert len(list((output / "attempts").glob("*.json"))) == 96
    incident_hashes = {}
    for attempt in (output / "attempts").glob("*.json"):
        result = ck.load_sealed(attempt)
        incident = json.loads(result["messages"][1]["content"])["incident"]
        incident_hashes.setdefault(result["case_id"], set()).add(ck.digest(incident))
    assert all(len(hashes) == 1 for hashes in incident_hashes.values())
    ck.train(output, fake_model, settings=SETTINGS)
    ck.evaluate(output, fake_model, 2, settings=SETTINGS)
    assert len(collections) == 32


def test_import_shared_evidence_rejects_reuse_across_evaluations(tmp_path):
    with pytest.raises(ck.EvaluationError, match="collect historical telemetry"):
        ck.import_shared_evidence(
            tmp_path / "new-evaluation",
            tmp_path / "previous-evaluation",
        )


def test_shared_inference_requires_complete_collection(workspace):
    _, output, _ = workspace
    shared_mode(output)
    ck.freeze(output)
    with pytest.raises(ck.EvaluationError, match="Run collect"):
        ck.train(output, fake_model, settings=SETTINGS)
    assert not list((output / "private/model_calls").glob("*.json"))
    assert ck.main(["collect", "--output-dir", str(output)]) == 1


def test_shared_collection_resumes_without_requerying_finished_cases(
    workspace, monkeypatch
):
    _, output, _ = workspace
    shared_mode(output)
    ck.freeze(output)
    calls = []
    collect = shared_collector(monkeypatch, calls)

    def interrupted(
        manifest,
        bundle,
        config,
        client_factory=None,
        historical_observability=None,
    ):
        if bundle["case_id"] == "case-003":
            raise KeyboardInterrupt("collection interrupted")
        return collect(
            manifest,
            bundle,
            config,
            client_factory,
            historical_observability,
        )

    monkeypatch.setattr(ck, "collect_live_sources", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ck.collect_shared(output)
    assert calls == ["case-001", "case-002"]
    assert not (output / "shared_evidence.json").exists()
    monkeypatch.setattr(ck, "collect_live_sources", collect)
    index = ck.collect_shared(output)
    assert len(index["records"]) == 35
    assert len(calls) == 35


def test_shared_backend_errors_block_inference_without_fallback(workspace, monkeypatch):
    _, output, _ = workspace
    shared_mode(output)
    ck.freeze(output)
    calls = []
    shared_collector(monkeypatch, calls, fail=True)
    with pytest.raises(ck.EvaluationError, match="backend errors"):
        ck.collect_shared(output)
    index = ck.load_sealed(output / "shared_evidence.json")
    assert len(index["collection_error_cases"]) == 35
    with pytest.raises(ck.EvaluationError, match="backend errors"):
        ck.train(output, fake_model, settings=SETTINGS)
    assert not list((output / "private/model_calls").glob("*.json"))


def test_shared_index_detects_resealed_bundle_change(workspace, monkeypatch):
    _, output, _ = workspace
    shared_mode(output)
    snapshot = ck.freeze(output)
    shared_collector(monkeypatch, [])
    ck.collect_shared(output)
    path = output / "private/live_evidence/shared-case-001.json"
    record = ck.load_sealed(path)
    record.pop("sha256")
    record["bundle"]["evidence"] = []
    path.write_text(ck.canonical(ck.seal(record)))
    with pytest.raises(ck.EvaluationError, match="changed after collection"):
        ck.stage_bundle(
            output, ck.load_manifest(output), snapshot, "case-001", "ignored"
        )


def test_reference_enum_bounded_repair_and_cached_audit(tmp_path):
    calls = []
    messages = [{"role": "user", "content": "No experimental label supplied"}]

    def model(current, schema):
        calls.append(current)
        refs = schema["$defs"]["Finding"]["properties"]["evidence_ids"]["items"]["enum"]
        assert refs == ["E0001"]
        response = fake_model(current, schema)
        if len(calls) == 1:
            response["content"] = response["content"].replace("E0001", "E014")
        return response

    kwargs = {
        "evidence": [{"id": "E0001"}],
        "max_repairs": 1,
        "audit_prefix": tmp_path / "case-observations",
    }
    parsed, response = ck.model_json(model, messages, ck.Observations, **kwargs)
    assert parsed.model_dump()["findings"][0]["evidence_ids"] == ["E0001"]
    assert len(calls) == 2
    assert "E014" in calls[1][-1]["content"]
    assert "experimental_label" not in ck.canonical(calls[1])
    assert response["usage"]["total_tokens"] == 20
    assert response["contract_validation"]["first_pass_valid"] is False
    assert response["contract_validation"]["repairs_used"] == 1
    assert len(list(tmp_path.glob("case-observations-*.json"))) == 2
    _, cached = ck.model_json(model, messages, ck.Observations, **kwargs)
    assert len(calls) == 2
    assert cached == response


def test_terminal_training_failures_do_not_restart_on_resume(workspace, monkeypatch):
    _, output, _ = workspace
    manifest_path = output / "manifest.json"
    manifest = ck.load_manifest(output)
    manifest.pop("sha256")
    manifest["max_repairs"] = 1
    manifest_path.write_text(ck.canonical(ck.seal(manifest)))
    snapshot = ck.freeze(output)

    def fixture_bundle(_output, _manifest, _snapshot, case_id, _collection_key):
        return snapshot["bundles"][case_id]

    monkeypatch.setattr(ck, "stage_bundle", fixture_bundle)
    calls = []

    def invalid(messages, schema):
        calls.append(schema["title"])
        result = fake_model(messages, schema)
        result["content"] = result["content"].replace("E0001", "E9999")
        return result

    report = ck.train(output, invalid, settings=SETTINGS)
    assert len(calls) == 34
    assert report["support_cases"] == 17
    assert report["errors"] == 17
    assert report["drafts"] == 0
    assert len(list((output / "private/training_errors").glob("*.json"))) == 17
    assert ck.train(output, invalid, settings=SETTINGS) == report
    assert len(calls) == 34
    assert not (output / "knowledge.json").exists()


def test_diagnosis_references_and_choices_constrained(workspace, tmp_path):
    _, output, _ = workspace
    manifest = ck.load_manifest(output)

    def invalid(messages, schema):
        assert schema["properties"]["fault_type"]["enum"] == list(ck.FAULTS)
        assert schema["properties"]["primary_service"]["anyOf"] == [
            {"type": "string", "enum": manifest["contract"]["known_services"]},
            {"type": "null"},
        ]
        assert schema["$defs"]["Alternative"]["properties"]["supporting_evidence"][
            "items"
        ]["enum"] == ["E0001"]
        result = fake_model(messages, schema)
        value = json.loads(result["content"])
        value["alternatives"][0]["supporting_evidence"] = ["K-case-002:E0001"]
        result["content"] = json.dumps(value)
        return result

    with pytest.raises(ck.ModelOutputError, match="K-case-002"):
        ck.model_json(
            invalid,
            [],
            ck.Diagnosis,
            evidence=[{"id": "E0001"}],
            contract=manifest["contract"],
            max_repairs=1,
            audit_prefix=tmp_path / "diagnosis",
        )
    assert len(list(tmp_path.glob("diagnosis-*.json"))) == 2


def test_paired_evaluation_counts_repairs_cost_and_keeps_all_cases(workspace):
    _, output, _ = workspace
    path = output / "manifest.json"
    manifest = ck.load_manifest(output)
    manifest.pop("sha256")
    manifest["max_repairs"] = 1
    path.write_text(ck.canonical(ck.seal(manifest)))
    reviewed(output)

    def repair_needed(messages, schema):
        result = fake_model(messages, schema)
        if len(messages) == 2:
            result["content"] = result["content"].replace("E0001", "E014")
        return result

    report = ck.evaluate(output, repair_needed, 1, settings=SETTINGS)
    for condition in ck.CONDITIONS:
        row = report["summary"][condition]
        assert row["cases"] == 18
        assert row["attempts"] == 18
        assert row["errors"] == 0
        assert row["contract_first_pass_failures"] == 18
        assert row["contract_repairs_used"] == 18
        assert row["reported_total_tokens"] == 360
    assert len(list((output / "attempts").glob("*.json"))) == 54


@pytest.mark.parametrize(
    "allowed,tool,http_status,expected",
    [
        (True, "tempo_analyze_trace", 404, "unavailable"),
        (False, "tempo_analyze_trace", 404, "tool_error"),
        (True, "tempo_analyze_trace", 503, "tool_error"),
        (True, "tempo_search_traces", 404, "tool_error"),
        (True, "prometheus_range_query", 404, "tool_error"),
    ],
)
def test_only_explicit_trace_detail_404_is_an_accepted_gap(
    workspace, allowed, tool, http_status, expected
):
    _, output, store = workspace
    manifest = ck.load_manifest(output)
    manifest["allow_missing_trace_details"] = allowed
    calls = []

    class MissingTrace(FakeTelemetry):
        def call_tool_json(self, name, arguments):
            if name == tool:
                return {
                    "status": "error",
                    "error": f"Client error '{http_status} "
                    f"{'Not Found' if http_status == 404 else 'Service Unavailable'}' "
                    "for url 'http://tempo/api/traces/missing'",
                }
            if name == "tempo_search_traces":
                return {
                    "traces": [
                        {
                            "traceID": "missing",
                            "startTimeUnixNano": str(
                                int(ck.timestamp(WINDOWS["fault"]["start"]) * 1e9)
                            ),
                            "durationMs": 1,
                        }
                    ]
                }
                if name == "tempo_analyze_trace":
                    return {"span_count": 1}
                return super().call_tool_json(name, arguments)

    config = ck.read_json(next(store.glob("*/effective_spec.json")))["observability"]
    raw = ck.collect_live_sources(
        manifest,
        ck.make_bundle("case-x", sources()),
        config,
        lambda **kwargs: MissingTrace(calls),
    )
    selected = [
        call for call in raw["live_collection"]["calls"] if call["tool"] == tool
    ]
    assert selected
    assert all(call["response"]["status"] == expected for call in selected)
    bundle = ck.make_bundle("case-x", raw)
    if expected == "unavailable":
        missing = [e for e in bundle["evidence"] if e["status"] == "unavailable"]
        assert len(missing) == 1
        assert missing[0]["fault_observation_count"] == 0
        assert missing[0]["observation_count"] == 0
        assert "critical_path_raw" not in missing[0]
        assert selected[0]["response"]["backend_response"]["status"] == "error"
