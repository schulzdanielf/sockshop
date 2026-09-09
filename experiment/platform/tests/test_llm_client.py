"""Tests for the LLM client abstraction used by the RCA platform."""
from __future__ import annotations

from experiment.platform.backend.analysis import llm_client


def test_resolve_api_style_from_openai_url():
    style = llm_client._resolve_api_style(
        "http://localhost:8001/v1/chat/completions",
        None,
    )
    assert style == "openai"


def test_messages_to_prompt_collapses_chat_history():
    prompt = llm_client._messages_to_prompt(
        [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Summarize the incident."},
        ]
    )
    assert "[SYSTEM]" in prompt
    assert "[USER]" in prompt
    assert prompt.endswith("[ASSISTANT]\n")


def test_extract_openai_text_reads_first_choice():
    text = llm_client._extract_openai_text(
        {
            "choices": [
                {"message": {"role": "assistant", "content": "final answer"}}
            ]
        }
    )
    assert text == "final answer"


def test_llm_health_uses_root_of_openai_endpoint(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"status":"ok"}'

    def _fake_urlopen(req, timeout):
        captured["url"] = getattr(req, "full_url", req)
        return _Resp()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", _fake_urlopen)
    out = llm_client.llm_health("http://localhost:8001/v1/chat/completions")

    assert out["ok"] is True
    assert captured["url"] == "http://localhost:8001/health"
