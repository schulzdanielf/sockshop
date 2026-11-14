import pytest

from experiment.eval.memory_hog import controlled_campaign, supplemental_campaign
from experiment.eval.memory_hog import supplemental_training_import as importer
from experiment.eval.memory_hog import training_b_rebuild
from experiment.platform.backend import rca_knowledge


def test_phase_windows_preserve_fault_timing_provenance():
    observations = [
        {
            "phase": "baseline",
            "start": "2026-11-09T23:41:00Z",
            "end": "2026-11-09T23:42:00Z",
        },
        {
            "phase": "intervention",
            "start": "2026-11-09T23:43:00Z",
            "end": "2026-11-09T23:44:00Z",
        },
        {
            "phase": "post",
            "start": "2026-11-09T23:45:00Z",
            "end": "2026-11-09T23:46:00Z",
        },
    ]
    report = {
        "observations": observations,
        "timestamps": {
            "intervention_estimate": {
                "start": observations[1]["start"],
                "end": observations[1]["end"],
                "source": "observed-qdisc-window",
                "duration_verified": False,
                "limitation": "observation bounds the intervention",
            }
        },
    }

    windows, provenance = importer.phase_windows(report)

    assert windows["fault"] == {
        "start": observations[1]["start"],
        "end": observations[1]["end"],
    }
    assert provenance["fault"]["timing_source"] == "observed-qdisc-window"
    assert provenance["fault"]["duration_verified"] is False
    assert provenance["fault"]["limitation"] == "observation bounds the intervention"


def test_phase_windows_rejects_mismatched_fault_estimate():
    report = {
        "observations": [
            {"phase": "baseline", "start": "2026-11-09T00:00:00Z", "end": "2026-11-09T00:01:00Z"},
            {"phase": "intervention", "start": "2026-11-09T00:02:00Z", "end": "2026-11-09T00:03:00Z"},
            {"phase": "post", "start": "2026-11-09T00:04:00Z", "end": "2026-11-09T00:05:00Z"},
        ],
        "timestamps": {
            "intervention_estimate": {
                "start": "2026-11-09T00:02:30Z",
                "end": "2026-11-09T00:03:00Z",
            }
        },
    }

    with pytest.raises(ValueError, match="differs from its recorded timing estimate"):
        importer.phase_windows(report)


def test_merged_base_preserves_parent_and_records_skill_lineage():
    cells = {
        (service, fault)
        for service in controlled_campaign.SERVICES
        for fault in controlled_campaign.FAULTS
    }
    supplemental_cells = {
        (case["service"], case["chaos_type"])
        for case in supplemental_campaign.CASES
        if case["phase"] == "train"
    }
    old_skill, active_skill = "a" * 64, "b" * 64
    parent_pairs = []
    for index, (service, fault) in enumerate(sorted(cells - supplemental_cells)):
        parent_pairs.append({
            "pair_id": f"parent-pair-{index}",
            "source_run_id": f"parent-run-{index}",
            "skill_hash": old_skill,
            "labels": {"service": service, "fault_type": fault},
            "sha256": f"{index:064x}",
        })
    parent_run_ids = [pair["source_run_id"] for pair in parent_pairs]
    parent_payload = {
        "version": rca_knowledge.VERSION,
        "mode": "exploratory",
        "model": rca_knowledge.QWEN35_MODEL_ID,
        "b_rebuild_version": training_b_rebuild.TRAINING_B_SCHEMA,
        "embedding_provider": "test-provider",
        "embedding_dim": 1,
        "skill_sha256": old_skill,
        "training_run_ids": parent_run_ids,
        "pairs": parent_pairs,
        "index": {
            pair["pair_id"]: [float(index)]
            for index, pair in enumerate(parent_pairs)
        },
    }
    parent = {
        **parent_payload,
        "sha256": rca_knowledge.digest(parent_payload),
    }
    new_pairs = []
    vectors = {}
    for index, case in enumerate(
        case for case in supplemental_campaign.CASES if case["phase"] == "train"
    ):
        pair = {
            "pair_id": f"supplemental-pair-{index}",
            "source_run_id": f"supplemental-run-{index}",
            "split": "train",
            "labels": {
                "service": case["service"],
                "fault_type": case["chaos_type"],
                "causal_category": "test-category",
            },
            "skill_hash": active_skill,
            "effect_validated": True,
            "b_rebuild_version": training_b_rebuild.TRAINING_B_SCHEMA,
            "guide_status": "procedural_pending",
        }
        pair["sha256"] = rca_knowledge.digest(pair)
        new_pairs.append(pair)
        vectors[pair["pair_id"]] = [float(index + 1)]
    metadata = {
        "protocol_version": importer.PROTOCOL_VERSION,
        "parent_base_sha256": parent["sha256"],
        "parent_training_run_ids": parent_run_ids,
        "parent_skill_sha256": old_skill,
        "source_hashes": {
            "parent_base_content_sha256": parent["sha256"],
        },
    }

    merged = importer.merged_base(
        parent,
        new_pairs,
        vectors,
        skill_sha256=active_skill,
        import_metadata=metadata,
    )

    assert len(merged["pairs"]) == 35
    assert merged["parent_base_sha256"] == parent["sha256"]
    assert merged["supplemental_training_import"]["parent_skill_sha256"] == old_skill
    assert merged["supplemental_training_import"]["parent_training_run_ids"] == parent_run_ids
    assert merged["pairs"][:32] == parent_pairs
    assert all(
        rca_knowledge._pair_skill_hash_compatible(merged, pair)
        for pair in parent_pairs
    )
    assert all(
        rca_knowledge._pair_skill_hash_compatible(merged, pair)
        for pair in new_pairs
    )


def test_index_bypass_marks_only_unreviewed_supplemental_pairs():
    parent_pairs = [
        {
            "pair_id": f"parent-pair-{index}",
            "source_run_id": f"parent-run-{index}",
            "review_status": rca_knowledge.BLIND_LIVE_REVIEW_STATUS,
            "review_manifest_sha256": "a" * 64,
        }
        for index in range(32)
    ]
    supplemental_pairs = [
        {
            "pair_id": f"supplemental-pair-{index}",
            "source_run_id": f"supplemental-run-{index}",
            "guide_status": (
                "procedurally_validated" if index == 0 else "procedural_pending"
            ),
            "b_procedural_status": "passed" if index == 0 else "pending",
        }
        for index in range(3)
    ]
    payload = {
        "training_run_ids": [
            pair["source_run_id"]
            for pair in [*parent_pairs, *supplemental_pairs]
        ],
        "pairs": [*parent_pairs, *supplemental_pairs],
        "supplemental_training_import": {
            "training_runs": [
                {"run_id": pair["source_run_id"]}
                for pair in supplemental_pairs
            ],
        },
    }
    candidate = {**payload, "sha256": rca_knowledge.digest(payload)}

    with pytest.raises(ValueError, match="independent blind-live adjudication"):
        importer._prepare_index_base(
            candidate,
            bypass_blind_review=False,
        )

    indexed = importer._prepare_index_base(
        candidate,
        bypass_blind_review=True,
    )

    assert candidate["sha256"] != indexed["sha256"]
    assert "blind_live_review_bypass" not in candidate
    assert indexed["blind_live_review_bypass"]["candidate_base_sha256"] == candidate["sha256"]
    marked = indexed["pairs"][-3:]
    assert all(
        pair["review_status"]
        == rca_knowledge.BLIND_LIVE_REVIEW_BYPASSED_STATUS
        and "review_manifest_sha256" not in pair
        for pair in marked
    )
    assert [pair["guide_status"] for pair in marked] == [
        "procedurally_validated",
        "procedural_pending",
        "procedural_pending",
    ]
    assert rca_knowledge.validated_blind_review_bypass_source_run_ids(indexed) == {
        "supplemental-run-0",
        "supplemental-run-1",
        "supplemental-run-2",
    }


def test_blind_review_case_omits_labels_ids_and_supervised_b():
    pair = {
        "source_run_id": "run-private",
        "pair_id": "pair-private",
        "labels": {
            "service": "orders",
            "fault_type": "container-kill",
            "causal_category": "container-lifecycle",
        },
        "phases": {"fault": {"start": "2026-11-09T00:00:00Z", "end": "2026-11-09T00:01:00Z"}},
        "a": {"observed_services": ["orders"], "symptoms": []},
        "b": {"diagnosis": {"fault_type": "container-kill"}},
        "evidence": [
            {
                "id": "N001",
                "tool": "get_run_golden_signals",
                "source_run_id": "run-private",
                "result_ref": "/private/run-private.json",
                "digest": {"status": "ok"},
            }
        ],
        "b_collection_evidence": [
            {
                "id": "N001",
                "tool": "check_pod_restarts",
                "pair_id": "pair-private",
                "result_ref": "/private/pair-private.json",
                "digest": {"status": "ok"},
            }
        ],
    }

    projected = importer._blind_review_case(pair, "case-0123456789ab")
    serialized = str(projected)

    assert "labels" not in projected
    assert "candidate_guide" not in projected
    assert "container-kill" not in serialized
    assert "run-private" not in serialized
    assert "pair-private" not in serialized
    assert "/private/" not in serialized
