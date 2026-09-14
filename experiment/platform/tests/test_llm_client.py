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
        {"choices": [{"message": {"role": "assistant", "content": "final answer"}}]}
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

    class _MockResponse:
        text = '{"reasoning": "Analysis...", "rca": "carts", "fault_category": "memory-exhaustion"}'

    class _MockModels:
        def generate_content(self, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return _MockResponse()

    class _MockClient:
        def __init__(self, api_key, http_options=None):
            captured["api_key"] = api_key
            captured["http_options"] = http_options
            self.models = _MockModels()

    import sys
    from types import ModuleType

    mock_google = ModuleType("google")
    mock_genai = ModuleType("genai")
    mock_genai.Client = _MockClient
    mock_types = ModuleType("types")
    mock_types.GenerateContentConfig = lambda **kwargs: kwargs
    mock_types.HttpOptions = lambda **kwargs: kwargs
    mock_types.ThinkingConfig = lambda **kwargs: kwargs
    mock_genai.types = mock_types
    mock_errors = ModuleType("errors")
    mock_errors.APIError = Exception
    mock_genai.errors = mock_errors
    mock_google.genai = mock_genai

    monkeypatch.setitem(sys.modules, "google", mock_google)
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setitem(sys.modules, "google.genai.errors", mock_errors)

    out = llm_client.call_llm_messages(
        [{"role": "user", "content": "test"}],
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        api_key="AIzaSy_test_key_123",
        api_style="gemini",
    )

    assert "carts" in out
    parsed = llm_client.parse_verdict_response(out)
    assert parsed["parse_error"] is None
    assert parsed["rca"] == "carts"
    assert parsed["fault_category"] == "memory-exhaustion"
    assert captured["api_key"] == "AIzaSy_test_key_123"
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["http_options"]["timeout"] == 120000
    assert captured["config"]["response_mime_type"] == "application/json"
    assert captured["config"]["max_output_tokens"] == 2048
    assert captured["config"]["thinking_config"] == {
        "include_thoughts": False,
        "thinking_budget": 0,
    }


def test_call_llm_messages_gemini_does_not_retry_quota_by_default(monkeypatch):
    calls = []
    sleeps = []

    class _MockApiError(Exception):
        code = 429
        message = "RESOURCE_EXHAUSTED: quota exceeded for API key AIzaSy_test_key_123"

    class _MockModels:
        def generate_content(self, model, contents, config):
            calls.append((model, contents, config))
            raise _MockApiError(_MockApiError.message)

    class _MockClient:
        def __init__(self, api_key, http_options=None):
            self.api_key = api_key
            self.http_options = http_options
            self.models = _MockModels()

    import sys
    from types import ModuleType

    mock_google = ModuleType("google")
    mock_genai = ModuleType("genai")
    mock_genai.Client = _MockClient
    mock_types = ModuleType("types")
    mock_types.GenerateContentConfig = lambda **kwargs: kwargs
    mock_types.HttpOptions = lambda **kwargs: kwargs
    mock_types.ThinkingConfig = lambda **kwargs: kwargs
    mock_genai.types = mock_types
    mock_errors = ModuleType("errors")
    mock_errors.APIError = _MockApiError
    mock_genai.errors = mock_errors
    mock_google.genai = mock_genai

    monkeypatch.setitem(sys.modules, "google", mock_google)
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setitem(sys.modules, "google.genai.errors", mock_errors)
    monkeypatch.setattr(llm_client, "DEFAULT_GEMINI_MAX_RETRIES", 3)
    monkeypatch.setattr(llm_client, "DEFAULT_GEMINI_RETRY_RATE_LIMITS", False)

    def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(llm_client.time, "sleep", _record_sleep)

    try:
        llm_client.call_llm_messages(
            [{"role": "user", "content": "test"}],
            url="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            api_key="AIzaSy_test_key_123",
            api_style="gemini",
        )
    except llm_client.LLMClientError as exc:
        assert "not retrying" in str(exc)
        assert "AIzaSy_test_key_123" not in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected Gemini quota error")

    assert len(calls) == 1
    assert sleeps == []


def test_call_llm_messages_gemini_retries_transient_once(monkeypatch):
    calls = []
    sleeps = []

    class _MockApiError(Exception):
        code = 503
        message = "model overloaded"

    class _MockResponse:
        text = '{"reasoning": "retry ok", "rca": "orders"}'

    class _MockModels:
        def generate_content(self, model, contents, config):
            calls.append((model, contents, config))
            if len(calls) == 1:
                raise _MockApiError(_MockApiError.message)
            return _MockResponse()

    class _MockClient:
        def __init__(self, api_key, http_options=None):
            self.api_key = api_key
            self.http_options = http_options
            self.models = _MockModels()

    import sys
    from types import ModuleType

    mock_google = ModuleType("google")
    mock_genai = ModuleType("genai")
    mock_genai.Client = _MockClient
    mock_types = ModuleType("types")
    mock_types.GenerateContentConfig = lambda **kwargs: kwargs
    mock_types.HttpOptions = lambda **kwargs: kwargs
    mock_types.ThinkingConfig = lambda **kwargs: kwargs
    mock_genai.types = mock_types
    mock_errors = ModuleType("errors")
    mock_errors.APIError = _MockApiError
    mock_genai.errors = mock_errors
    mock_google.genai = mock_genai

    monkeypatch.setitem(sys.modules, "google", mock_google)
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)
    monkeypatch.setitem(sys.modules, "google.genai.errors", mock_errors)
    monkeypatch.setattr(llm_client, "DEFAULT_GEMINI_MAX_RETRIES", 1)
    monkeypatch.setattr(llm_client, "DEFAULT_GEMINI_RETRY_DELAY", 2.0)
    monkeypatch.setattr(llm_client, "DEFAULT_GEMINI_RETRY_RATE_LIMITS", False)

    def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(llm_client.time, "sleep", _record_sleep)

    out = llm_client.call_llm_messages(
        [{"role": "user", "content": "test"}],
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        api_key="AIzaSy_test_key_123",
        api_style="gemini",
    )

    assert out == _MockResponse.text
    assert len(calls) == 2
    assert sleeps == [2.0]


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
    assert (
        captured["headers"].get("User-agent") == "MicroservicesDemo-RCA/1.0"
        or captured["headers"].get("User-Agent") == "MicroservicesDemo-RCA/1.0"
    )


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
