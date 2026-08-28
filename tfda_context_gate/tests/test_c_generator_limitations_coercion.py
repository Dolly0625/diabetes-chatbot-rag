from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tfda_context_gate.c_generator.langchain_adapter import LangChainCV2Generator, _is_retryable_exception, _rich_parsing_error_message
from tfda_context_gate.c_generator.schemas import EvidenceAwareV2Answer, V2SupportedClaim
from tfda_context_gate.c_generator.c_workflow_input import CWorkflowInput


def _base_answer_kwargs():
    return {
        "decision": "ANSWER",
        "answer": "測試回答",
        "supported_claims": [V2SupportedClaim(claim_id="c1", claim="測試主張", evidence_ids=["E1"])],
        "unsupported_requests": [],
    }


def test_limitations_str_coerced_to_list():
    obj = EvidenceAwareV2Answer(**_base_answer_kwargs(), limitations="單一限制字串")  # type: ignore[arg-type]
    assert obj.limitations == ["單一限制字串"]


def test_limitations_none_coerced_to_empty():
    obj = EvidenceAwareV2Answer(**_base_answer_kwargs(), limitations=None)  # type: ignore[arg-type]
    assert obj.limitations == []


def test_limitations_empty_str_coerced_to_empty():
    obj = EvidenceAwareV2Answer(**_base_answer_kwargs(), limitations="")  # type: ignore[arg-type]
    assert obj.limitations == []


def test_limitations_list_unchanged():
    obj = EvidenceAwareV2Answer(**_base_answer_kwargs(), limitations=["a", "b"])
    assert obj.limitations == ["a", "b"]


def test_limitations_str_via_model_validate():
    data = {**_base_answer_kwargs(), "limitations": "模型驗證字串"}
    obj = EvidenceAwareV2Answer.model_validate(data)
    assert obj.limitations == ["模型驗證字串"]


def test_limitations_none_via_model_validate():
    data = {**_base_answer_kwargs(), "limitations": None}
    obj = EvidenceAwareV2Answer.model_validate(data)
    assert obj.limitations == []


def test_parsing_error_transparency_includes_details():
    fake_raw = SimpleNamespace(tool_calls=[{"name": "EvidenceAwareV2Answer", "args": {"limitations": "str"}}], content=None, additional_kwargs={"tool_calls": [{"function": {"arguments": '{"limitations":"str"}'}}]})
    response = {"raw": fake_raw, "parsed": None, "parsing_error": ValidationError.from_exception_data("EvidenceAwareV2Answer", [{"type": "list_type", "loc": ("limitations",), "msg": "Input should be a valid list", "input": "str"}])}
    msg = _rich_parsing_error_message(response, "C v2 structured output did not contain parsed data")
    assert "parsing_error_type=ValidationError" in msg
    assert "limitations" in msg.lower()
    assert "raw_tool_calls" in msg


def test_parsing_error_empty_tool_calls_transparency():
    fake_raw = SimpleNamespace(tool_calls=[], content='{"decision":"ANSWER","answer":"..."}', additional_kwargs={})
    response = {"raw": fake_raw, "parsed": None, "parsing_error": None}
    msg = _rich_parsing_error_message(response, "C v2 structured output did not contain parsed data")
    assert "parsing_error_type=EmptyParsed" in msg
    assert "raw_tool_calls=[]" in msg
    assert "raw_content_preview" in msg


def test_langchain_adapter_generate_parsing_error_raises_rich_value_error():
    mock_chain = MagicMock()
    fake_raw = SimpleNamespace(tool_calls=[{"name": "EvidenceAwareV2Answer", "args": {"limitations": "str"}}], content="{}", additional_kwargs={})
    pe = ValidationError.from_exception_data("EvidenceAwareV2Answer", [{"type": "list_type", "loc": ("limitations",), "msg": "Input should be a valid list", "input": "str"}])
    mock_chain.invoke.return_value = {"raw": fake_raw, "parsed": None, "parsing_error": pe}
    gen = LangChainCV2Generator(mock_chain)
    req = CWorkflowInput(request_id="test-001", original_query="請說明糖尿病的一般飲食原則。", b_decision="PASS", approved_evidence_ids=["E1"], evidence=[])
    with pytest.raises(ValueError) as excinfo:
        gen.generate(req)
    msg = str(excinfo.value)
    assert "parsing_error_type=ValidationError" in msg
    assert "C v2 structured output did not contain parsed data" in msg


def test_is_retryable_only_network_errors():
    # ValidationError 不重試
    ve = ValidationError.from_exception_data("EvidenceAwareV2Answer", [{"type": "list_type", "loc": ("limitations",), "msg": "Input should be a valid list", "input": "str"}])
    assert _is_retryable_exception(ve) is False
    assert _is_retryable_exception(ValueError("some")) is False
    # TimeoutError 可重試
    assert _is_retryable_exception(TimeoutError("timeout")) is True
    # Mock openai APIConnectionError（若有安裝 openai）
    try:
        import openai

        assert _is_retryable_exception(openai.APIConnectionError(request=MagicMock())) is True
        assert _is_retryable_exception(openai.RateLimitError(message="rate limit", response=MagicMock(), body={})) is True
    except Exception:
        pass


def test_invoke_with_retry_only_retries_network_once():
    from tfda_context_gate.c_generator.langchain_adapter import _invoke_with_retry

    # ValidationError 不重試：只調用 1 次
    mock_chain = MagicMock()
    ve = ValidationError.from_exception_data("EvidenceAwareV2Answer", [{"type": "list_type", "loc": ("limitations",), "msg": "Input should be a valid list", "input": "str"}])
    mock_chain.invoke.side_effect = ve
    with pytest.raises(ValidationError):
        _invoke_with_retry(mock_chain, [])
    assert mock_chain.invoke.call_count == 1

    # Network error 重試 1 次：調用 2 次
    mock_chain2 = MagicMock()
    try:
        import openai

        err = openai.APIConnectionError(request=MagicMock())
    except Exception:
        err = TimeoutError("timeout")
    mock_chain2.invoke.side_effect = [err, {"raw": None, "parsed": {"decision": "ANSWER", "answer": "ok", "supported_claims": [], "unsupported_requests": [], "limitations": []}, "parsing_error": None}]
    # monkeypatch time.sleep to avoid 2s wait
    orig_sleep = time.sleep
    try:
        time.sleep = lambda x: None  # type: ignore[assignment]
        result = _invoke_with_retry(mock_chain2, [])
    finally:
        time.sleep = orig_sleep  # type: ignore[assignment]
    assert mock_chain2.invoke.call_count == 2
    assert result["parsed"] is not None


def test_formal_factory_uses_tool_choice_required(monkeypatch):
    import tfda_context_gate.workflow.formal_factory as formal_factory

    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured["llm_kwargs"] = kwargs

        def with_structured_output(self, schema, **kwargs):
            captured["with_kwargs"] = kwargs
            mock_chain = MagicMock()
            return mock_chain

    monkeypatch.setattr("langchain_openai.ChatOpenAI", FakeLLM)
    monkeypatch.setattr("tfda_context_gate.run_config.env_value", lambda k, d=None: "opencode/mimo-v2.5" if k == "ROUTER_LLM_MODEL" else "https://opencode.ai/zen/go/v1" if k == "OPENCODE_BASE_URL" else "fake-key" if k == "OPENCODE_API_KEY" else d)

    gen = formal_factory._build_formal_generator()
    assert "tool_choice" in captured["with_kwargs"] or "strict" in captured["with_kwargs"]
    if "tool_choice" in captured["with_kwargs"]:
        assert captured["with_kwargs"]["tool_choice"] == "required"
