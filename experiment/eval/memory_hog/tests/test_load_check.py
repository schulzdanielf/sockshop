import json
import time
from unittest.mock import Mock

import pytest
import yaml

from experiment.eval.memory_hog import controlled_campaign as campaign
from experiment.eval.memory_hog import load_check


@pytest.mark.parametrize("failure", [None, "collect", "traces", "cleanup", "sampler", "slow_queries"])
@pytest.mark.parametrize("level", ["medium", "rag"])
def test_single_load_rehearsal_never_injects_and_always_cleans_up(monkeypatch, tmp_path, failure, level):
    source = yaml.safe_load((campaign.HERE / "config.yaml").read_text())
    cfg = campaign.build_config(source, tmp_path, level, 42, {})
    original_config = json.dumps(cfg, sort_keys=True)
    engine = Mock()
    engine._stop_flags = {}
    engine.load.start_load.return_value = {"remote_pid": 42}
    engine.load.status.return_value = {"state": "running"}
    engine.load.stop_load.return_value = {"stopped": failure != "cleanup"}
    engine.load.collect_summary.return_value = {}
    clock = [0.]
    monkeypatch.setattr(load_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(load_check, "SAMPLE_INTERVAL_SECONDS", .01)
    engine._sleep_interruptible.side_effect = lambda flag, seconds: clock.__setitem__(0, clock[0] + seconds)
    monkeypatch.setattr(load_check, "OrchestratorEngine", Mock(return_value=engine))
    monkeypatch.setattr(load_check, "SqliteStorage", Mock())
    monkeypatch.setattr(campaign, "require_environment", Mock())
    created = []

    def controlled_run(engine, run_id, spec):
        assert "knowledge_campaign" not in spec
        run = Mock()
        run.run_id, run.service = run_id, spec["controlled_campaign"]["service"]
        run.target = {"container_id": "original", "limits": {}}
        run.application_container_status.return_value = {"container_id": "original"}
        run.spec = {"observability": {"metrics": {"mcp_sse_url": "http://mcp/sse"}}}
        run.raw = {f"stabilization_{i}": {"data": {}} for i in range(9)}
        run.health.return_value = {"healthy": True}
        run.collect.return_value = {
            "memory_usage_bytes": {"max": 100}, "memory_limit_bytes": {"mean": 1000},
        }
        if failure == "collect":
            run.collect.side_effect = RuntimeError("metrics unavailable")
        elif failure == "sampler":
            run.sample_cgroup.side_effect = RuntimeError("cgroup read unavailable")
        elif failure == "slow_queries":
            def slow_collect(*args):
                before = run.sample_cgroup.call_count
                time.sleep(.04)
                assert run.sample_cgroup.call_count > before
                return {"memory_usage_bytes": {"max": 100}, "memory_limit_bytes": {"mean": 1000}}
            run.collect.side_effect = slow_collect
        created.append(run)
        return run

    monkeypatch.setattr(load_check, "ControlledRun", controlled_run)
    monkeypatch.setattr(load_check, "stability_metrics", lambda data: {"cpu": {"passed": True}})
    monkeypatch.setattr(load_check, "resource_environment", Mock(return_value={}))
    client = Mock()
    client.call_tool_json.return_value = {"traces": [] if failure == "traces" else [{"traceID": "fresh"}]}
    monkeypatch.setattr(load_check, "MCPToolClient", Mock(return_value=client))
    directory = tmp_path / "load-check-test"
    if failure in {"collect", "cleanup", "sampler"}:
        with pytest.raises(RuntimeError):
            load_check.run_load_check(cfg, directory)
    else:
        result = load_check.run_load_check(cfg, directory)
        assert result["passed"] is (failure in {None, "slow_queries"})
    engine.load.start_load.assert_called_once()
    engine.load.stop_load.assert_called_once()
    engine.chaos.inject.assert_not_called()
    assert all(not run.run.called and not run.intervene.called for run in created)
    assert json.dumps(cfg, sort_keys=True) == original_config
    report = json.loads((directory / "report.json").read_text())
    assert report["injections_submitted"] == 0
    assert report["users"] == 30
    assert report["load_stopped"]["stopped"] is (failure != "cleanup")
    if failure and failure != "slow_queries":
        assert report["passed"] is False


@pytest.mark.parametrize("args", [
    ["--load-check", "--run"], ["--load-check", "--preflight"],
    ["--load-check", "--resume", "campaign"],
])
def test_load_check_cannot_be_combined_with_campaign_execution(args):
    with pytest.raises(SystemExit) as exc:
        campaign.main(args)
    assert exc.value.code == 2
