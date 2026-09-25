from experiment.platform.backend.engine import _build_observation_window
from experiment.platform.backend.engine import _build_partial_observation_window
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
    result = _build_observation_window(
        run_id="run-abc123",
        experiment_id="memory-hog",
        status="completed",
        timings=TIMINGS,
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