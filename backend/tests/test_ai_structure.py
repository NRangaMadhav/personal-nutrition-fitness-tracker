from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from requests.exceptions import ConnectionError, Timeout
from urllib3.exceptions import MaxRetryError, ProtocolError

from backend.app.ai import providers
from backend.app.ai.providers import LangChainAdapter, ProviderUnavailable
from backend.app.ai.schemas import Extraction


def adapter_with(*responses):
    adapter = LangChainAdapter.__new__(LangChainAdapter)
    adapter.name = "nvidia"
    adapter.model = "mock-vision"
    adapter.structured_supported = False
    adapter.client = SimpleNamespace(invoke=Mock(side_effect=responses))
    return adapter


def test_raw_output_schema_is_repaired_once():
    adapter = adapter_with(
        AIMessage(content='{"events":[{"type":"water"}]}'),
        AIMessage(content='{"events":[{"type":"water","occurred_at":"2026-09-08T12:00:00Z","amount_ml":500}]}'),
    )
    result = adapter.invoke([HumanMessage(content="Water 500 ml")], Extraction)
    assert result.data.events[0].amount_ml == 500
    assert adapter.client.invoke.call_count == 2
    correction = adapter.client.invoke.call_args.args[0][-1].content
    assert "failed validation" in correction
    assert "occurred_at" in correction


def test_invalid_output_retry_is_bounded_and_logs_are_redacted(caplog):
    adapter = adapter_with(AIMessage(content="private input marker"), AIMessage(content="still not JSON"))
    with pytest.raises(ProviderUnavailable, match="invalid structured data"):
        adapter.invoke([HumanMessage(content="synthetic")], Extraction)
    assert adapter.client.invoke.call_count == 2
    assert "private input marker" not in caplog.text
    assert "AI output validation failed" in caplog.text


def test_schema_repair_does_not_repeat_transport_failures(caplog):
    adapter = adapter_with(Timeout("private provider detail"))
    with pytest.raises(ProviderUnavailable, match="could not complete"):
        adapter.invoke([HumanMessage(content="synthetic")], Extraction)
    assert adapter.client.invoke.call_count == 1
    assert "private provider detail" not in caplog.text


def test_nvidia_upload_timeout_is_not_five_second_connect_timeout(monkeypatch):
    captured = {}

    def request(self, method, url, **kwargs):
        captured.update(kwargs)
        return "sent"

    monkeypatch.setattr(providers.requests.Session, "request", request)
    with providers.BoundedNvidiaSession() as session:
        assert session.request("POST", "https://offline.test", json={"image": "synthetic"}) == "sent"
    assert captured["timeout"] == (30, 60)
    assert "timeout" not in captured["json"]


def test_wrapped_image_write_timeout_is_actionable_and_redacted(caplog):
    error = ConnectionError(MaxRetryError(
        None, "/private-input",
        reason=ProtocolError("Connection aborted.", TimeoutError("The write operation timed out")),
    ))
    adapter = adapter_with(error)
    with pytest.raises(ProviderUnavailable) as exc:
        adapter.invoke([HumanMessage(content="private photo")], Extraction)
    assert exc.value.code == "ai_upload_timeout"
    assert "ai_upload_timeout" in caplog.text
    assert "/private-input" not in caplog.text
    assert "private photo" not in str(exc.value)
    assert adapter.client.invoke.call_count == 1


def test_structured_transport_failure_does_not_trigger_another_nvidia_request():
    adapter = adapter_with()
    adapter.structured_supported = True
    adapter.client.with_structured_output = Mock(
        return_value=SimpleNamespace(invoke=Mock(side_effect=Timeout("private provider detail")))
    )
    with pytest.raises(ProviderUnavailable) as exc:
        adapter.invoke([], Extraction)
    assert exc.value.code == "ai_timeout"
    adapter.client.invoke.assert_not_called()


def test_typed_structured_mode_rejection_still_uses_portable_json():
    adapter = adapter_with(AIMessage(content='{"events": []}'))
    adapter.structured_supported = True
    adapter.client.with_structured_output = Mock(
        return_value=SimpleNamespace(invoke=Mock(side_effect=providers.StructuredOutputUnsupported("unsupported")))
    )
    assert adapter.invoke([], Extraction).data.events == []
    assert adapter.client.invoke.call_count == 1


def test_auto_keeps_real_failure_code_when_vision_fallback_is_unconfigured(monkeypatch):
    monkeypatch.setattr(providers, "get_settings", lambda: SimpleNamespace(ai_primary_provider="nvidia"))

    def get_adapter(name, **kwargs):
        if name == "nvidia":
            raise ProviderUnavailable("Upload timed out.", code="ai_upload_timeout")
        raise ProviderUnavailable("Vision not configured.", code="ai_not_configured")

    monkeypatch.setattr(providers, "get_adapter", get_adapter)
    with pytest.raises(ProviderUnavailable) as exc:
        providers.invoke_provider([], Extraction, provider="auto", vision=True)
    assert exc.value.code == "ai_upload_timeout"


@pytest.mark.parametrize("model,direct_mode", [
    ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", True),
    ("other-configured-model", False),
])
def test_direct_mode_is_specific_to_verified_nemotron_vision_model(monkeypatch, model, direct_mode):
    captured = {}

    def client(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(_client=SimpleNamespace(model=SimpleNamespace(supports_structured_output=False)))

    monkeypatch.setattr(providers, "ChatNVIDIA", client)
    providers.LangChainAdapter("nvidia", model, "test-only")
    assert captured["model_kwargs"] == (
        {"chat_template_kwargs": {"enable_thinking": False}} if direct_mode else {}
    )
