from experiment.platform.backend.engine import _build_observation_window
from experiment.platform.backend.engine import _build_partial_observation_window
from experiment.platform.backend.engine import _resolve_fault_window
from experiment.platform.backend.domain import DomainEvent


TIMINGS = {
    "t_start": "2026-09-25T10:00:00+00:00",
    "t_warmup_start": "2026-09-25T10:01:00+00:00",
    "t_warmup_end": "2026-09-25T10:02:00+00:00",
    "t_fault_start": "2026-09-25T10:02:00+00:00",
    "t_fault_end": "2026-09-25T10:04:00+00:00",
    "t_recovery_observed": "2026-09-25T10:08:00+00:00",
}


def test_observation_window_contains_run_and_phase_bounds():
    timings = dict(TIMINGS, fault_window_source="manifest_observed")
    result = _build_observation_window(
        run_id="run-abc123",
        experiment_id="memory-hog",
        status="completed",
        timings=timings,
        sampling_interval_seconds=15,
    )

    assert result["window"] == {
        "start": TIMINGS["t_start"],
        "end": TIMINGS["t_recovery_observed"],
    }
    assert result["phases"]["fault"] == {
        "start": TIMINGS["t_fault_start"],
        "end": TIMINGS["t_fault_end"],
    }
    assert result["phase_sources"]["fault"] == "manifest_observed"
    assert result["sampling_interval_seconds"] == 15


def test_observation_window_rejects_missing_global_bounds():
    timings = dict(TIMINGS)
    del timings["t_recovery_observed"]

    try:
        _build_observation_window(
            run_id="run-abc123",
            experiment_id="memory-hog",
            status="completed",
            timings=timings,
            sampling_interval_seconds=15,
        )
    except ValueError as exc:
        assert str(exc) == "observation window requires start and end timestamps"
    else:
        raise AssertionError("missing window end must be rejected")


def test_observation_window_preserves_controlled_campaign_phases_and_sources():
    controlled = {
        "timezone": "UTC",
        "sampling_interval_seconds": 5,
        "window": {
            "start": "2026-10-18T12:31:05+00:00",
            "end": "2026-10-18T12:39:18+00:00",
        },
        "phases": {
            "warmup": {"start": "2026-10-18T12:31:05+00:00", "end": "2026-10-18T12:32:54+00:00"},
            "baseline": {"start": "2026-10-18T12:32:54+00:00", "end": "2026-10-18T12:34:38+00:00"},
            "fault": {"start": "2026-10-18T12:34:41+00:00", "end": "2026-10-18T12:36:58+00:00"},
            "post": {"start": "2026-10-18T12:36:58+00:00", "end": "2026-10-18T12:39:18+00:00"},
        },
        "phase_sources": {
            "baseline": "after_load_stabilization",
            "fault": "virtualservice_api_bracket_estimated",
        },
    }
    result = _build_observation_window(
        run_id="run-controlled",
        experiment_id="controlled-experiment",
        status="completed",
        timings={"controlled_observation_window": controlled},
        sampling_interval_seconds=5,
    )

    assert result["window"] == controlled["window"]
    assert result["phases"] == controlled["phases"]
    assert result["phase_sources"] == controlled["phase_sources"]
    assert result["sampling_interval_seconds"] == 5


def test_observation_window_reconstructs_legacy_controlled_run_timings():
    timings = {
        "warmup_started": {"at": "2026-10-18T12:31:05+00:00"},
        "warmup_completed": {"at": "2026-10-18T12:32:54+00:00"},
        "baseline_started": {"at": "2026-10-18T12:32:54+00:00"},
        "baseline_completed": {"at": "2026-10-18T12:34:38+00:00"},
        "intervention_removed": {"at": "2026-10-18T12:36:58+00:00"},
        "recovery_observed": {"at": "2026-10-18T12:39:18+00:00"},
        "intervention_estimate": {
            "start": "2026-10-18T12:34:41+00:00",
            "end": "2026-10-18T12:36:58+00:00",
            "source": "virtualservice_api_bracket",
            "duration_verified": False,
        },
    }

    result = _build_observation_window(
        run_id="run-old-controlled",
        experiment_id="controlled-experiment",
        status="completed",
        timings=timings,
        sampling_interval_seconds=0,
    )

    assert result["window"] == {
        "start": timings["warmup_started"]["at"],
        "end": timings["recovery_observed"]["at"],
    }
    assert result["phases"]["fault"] == {
        "start": timings["intervention_estimate"]["start"],
        "end": timings["intervention_estimate"]["end"],
    }
    assert result["phase_sources"]["fault"] == "virtualservice_api_bracket"
    assert result["sampling_interval_seconds"] == 5


def test_fault_window_uses_litmus_injection_start_and_configured_duration():
    result = _resolve_fault_window(
        "2026-09-25T10:02:00+00:00",
        "2026-09-25T10:04:30+00:00",
        {
            "mode": "argo_workflow",
            "final_status": {
                "chaos_started_at": "2026-09-25T10:02:37+00:00",
                "chaos_window_confirmed": True,
            },
        },
        60,
    )

    assert result == (
        "2026-09-25T10:02:37+00:00",
        "2026-09-25T10:03:37+00:00",
        "litmus_result_created_at_plus_configured_duration",
    )


def test_fault_window_uses_manifest_apply_and_cleanup_timestamps():
    result = _resolve_fault_window(
        "2026-09-25T10:02:00+00:00",
        "2026-09-25T10:04:30+00:00",
        {
            "mode": "manifest",
            "injected_at": "2026-09-25T10:02:04+00:00",
            "stopped_at": "2026-09-25T10:03:04+00:00",
        },
        60,
    )

    assert result == (
        "2026-09-25T10:02:04+00:00",
        "2026-09-25T10:03:04+00:00",
        "manifest_observed",
    )


def test_fault_window_falls_back_when_actual_timestamps_are_unavailable():
    result = _resolve_fault_window(
        "2026-09-25T10:02:00+00:00",
        "2026-09-25T10:04:30+00:00",
        {"mode": "argo_workflow", "final_status": {"workflow_phase": "Succeeded"}},
        60,
    )

    assert result == (
        "2026-09-25T10:02:00+00:00",
        "2026-09-25T10:04:30+00:00",
        "planned_phase_fallback",
    )


def test_fault_window_does_not_treat_failed_litmus_creation_as_injection_start():
    result = _resolve_fault_window(
        "2026-09-25T10:02:00+00:00",
        "2026-09-25T10:04:30+00:00",
        {
            "mode": "argo_workflow",
            "final_status": {
                "chaos_started_at": None,
                "chaos_result_created_at": "2026-09-25T10:02:37Z",
                "chaos_phase": "Error",
                "chaos_window_confirmed": False,
            },
        },
        60,
    )

    assert result == (
        "2026-09-25T10:02:00+00:00",
        "2026-09-25T10:04:30+00:00",
        "planned_phase_fallback",
    )


def test_partial_observation_window_uses_events_and_marks_missing_phases():
    events = [
        DomainEvent("run-abc123", "run_started", "2026-09-25T10:00:00+00:00", {}),
        DomainEvent("run-abc123", "warmup_started", "2026-09-25T10:01:00+00:00", {}),
        DomainEvent("run-abc123", "warmup_completed", "2026-09-25T10:02:00+00:00", {}),
    ]

    result = _build_partial_observation_window(
        run_id="run-abc123",
        experiment_id="memory-hog",
        status="stopped",
        events=events,
        ended_at="2026-09-25T10:02:30+00:00",
        sampling_interval_seconds=15,
    )

    assert result["partial"] is True
    assert result["window"]["start"] == events[0].ts
    assert result["phases"] == {
        "baseline": {"start": events[0].ts, "end": events[1].ts},
        "warmup": {"start": events[1].ts, "end": events[2].ts},
    }
