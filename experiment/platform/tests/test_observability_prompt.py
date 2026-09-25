from experiment.platform.backend.analysis.prompt import assemble_prompt


def test_prompt_includes_observability_evidence_without_derived_rca_labels():
    prompt, _meta = assemble_prompt(
        {
            "target": {
                "run_id": "run-1",
                "summary_text": "Target summary",
                "tags": [],
                "observability_summary": {
                    "observation_window": {"window": {"start": "s", "end": "e"}},
                    "sources": {
                        "metrics": {"phase_metrics": {"latency": {"fault": {}}}},
                        "traces": {
                            "summary": {"trace_count": 2},
                            "affected_services": ["must-not-appear"],
                        },
                    },
                    "observed": {"verdict_signal": "must-not-appear"},
                    "gaps": {},
                },
            },
            "neighbours": [],
        },
        system_id=None,
    )

    assert "Observability context" in prompt
    assert '"trace_count": 2' in prompt
    assert "must-not-appear" not in prompt