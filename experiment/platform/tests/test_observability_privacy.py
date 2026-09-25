import json

from experiment.platform.backend.observability import otel


def test_llm_audit_redacts_content_by_default(tmp_path, monkeypatch):
    path = tmp_path / "llm.jsonl"
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    otel.configure_llm_io_audit(str(path))

    otel.record_llm_io({"prompt": "secret prompt", "response": "secret response"})

    row = json.loads(path.read_text(encoding="utf-8"))
    assert "prompt" not in row
    assert "response" not in row
    assert row["prompt_chars"] == len("secret prompt")
    assert row["response_chars"] == len("secret response")


def test_llm_audit_can_capture_content_when_explicitly_enabled(tmp_path, monkeypatch):
    path = tmp_path / "llm.jsonl"
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    otel.configure_llm_io_audit(str(path))

    otel.record_llm_io({"prompt": "visible prompt", "response": "visible response"})

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["prompt"] == "visible prompt"
    assert row["response"] == "visible response"