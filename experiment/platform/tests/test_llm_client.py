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


def test_resolve_api_style_from_gemini_url():
    style = llm_client._resolve_api_style(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
        None,
    )
    assert style == "gemini"


def test_call_llm_messages_gemini_format(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"candidates":[{"content":{"parts":[{"text":"{\\"rca\\":\\"carts\\"}"}]}}]}'

    def _fake_urlopen(req, timeout):
        captured["url"] = getattr(req, "full_url", req)
        return _Resp()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", _fake_urlopen)
    out = llm_client.call_llm_messages(
        [{"role": "user", "content": "test"}],
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
        api_key="AIzaSy_test_key_123",
        api_style="gemini",
    )

    assert out == '{"rca":"carts"}'
    assert "key=AIzaSy_test_key_123" in captured["url"]


def test_resolve_api_style_from_github_models_url():
    style = llm_client._resolve_api_style(
        "https://api.openai.com/v1/chat/completions",
        None,
    )
    assert style == "openai"


def test_call_llm_messages_sends_bearer_token(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"role":"assistant","content":"{\\"rca\\":\\"orders\\"}"}}]}'

    def _fake_urlopen(req, timeout):
        captured["headers"] = dict(req.headers)
        captured["url"] = getattr(req, "full_url", req)
        return _Resp()

    monkeypatch.setattr(llm_client.urllib.request, "urlopen", _fake_urlopen)
    out = llm_client.call_llm_messages(
        [{"role": "user", "content": "test"}],
        url="https://api.openai.com/v1/chat/completions",
        model="gpt-4o-mini",
        api_key="sk-test_token_123",
    )

    assert out == '{"rca":"orders"}'
    assert captured["headers"].get("Authorization") == "Bearer sk-test_token_123"
    assert captured["headers"].get("User-agent") == "MicroservicesDemo-RCA/1.0" or captured["headers"].get("User-Agent") == "MicroservicesDemo-RCA/1.0"


def test_llm_health_handles_cloud_endpoints():
    out = llm_client.llm_health(
        url="https://api.openai.com/v1/chat/completions",
        api_key="sk-test_token_123",
    )
    assert out["ok"] is True
    assert out["style"] == "openai_cloud"


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
