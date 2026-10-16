import io
import json
import sys
from urllib.error import HTTPError

from experiment.platform import rca_cli


def test_cli_calls_same_route_and_saves_json(monkeypatch, tmp_path, capsys):
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys, "argv", ["rca", "--run-id", "run-test", "--output", str(output)]
    )
    requests = []

    def open_request(request, timeout):
        requests.append((request.full_url, request.method, timeout))
        return io.BytesIO(
            json.dumps({"status": "inconclusive", "answer": "Lacuna"}).encode()
        )

    monkeypatch.setattr(rca_cli, "urlopen", open_request)
    assert rca_cli.main() == 0
    assert requests == [
        ("http://127.0.0.1:8010/api/runs/run-test/skill-rca", "POST", 900)
    ]
    assert json.loads(output.read_text())["status"] == "inconclusive"
    assert "inconclusive" in capsys.readouterr().out


def test_cli_http_error_reports_artifact(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rca", "--run-id", "run-test"])

    def fail(*args, **kwargs):
        raise HTTPError(
            "http://api",
            502,
            "failed",
            {},
            io.BytesIO(b'{"artifact_path":"saved.json"}'),
        )

    monkeypatch.setattr(rca_cli, "urlopen", fail)
    assert rca_cli.main() == 1
    assert "saved.json" in capsys.readouterr().err


def test_cli_does_not_overwrite_output(monkeypatch, tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("original")
    monkeypatch.setattr(
        sys, "argv", ["rca", "--run-id", "run-test", "--output", str(output)]
    )
    monkeypatch.setattr(
        rca_cli,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(b'{"status":"confirmed"}'),
    )
    assert rca_cli.main() == 1
    assert output.read_text() == "original"
