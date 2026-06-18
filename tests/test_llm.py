import pytest

from llm import guardrails
from llm.extraction_fallback import normalize_item_description
from llm.pr_type_classifier import classify_pr_type
from llm.trace import make_trace
from schemas.pr_schema import LLMFallbackTrace


def test_llm_disabled_by_default(monkeypatch):
    monkeypatch.delenv("IPRMS_LLM_FALLBACK_ENABLED", raising=False)
    assert guardrails.llm_enabled() is False


def test_llm_enabled_via_env(monkeypatch):
    monkeypatch.setenv("IPRMS_LLM_FALLBACK_ENABLED", "true")
    assert guardrails.llm_enabled() is True


def test_classify_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("IPRMS_LLM_FALLBACK_ENABLED", raising=False)
    assert classify_pr_type({"urgency": "normal"}) is None


def test_normalize_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("IPRMS_LLM_FALLBACK_ENABLED", raising=False)
    assert normalize_item_description("IT equipment") is None


def test_enabled_but_no_model_is_graceful(monkeypatch):
    # enabled but no model/credentials -> None (deterministic, no crash)
    monkeypatch.setenv("IPRMS_LLM_FALLBACK_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert classify_pr_type({"urgency": "normal"}) is None
    assert normalize_item_description("IT equipment") is None


def test_guardrails_allowed_points():
    guardrails.assert_allowed("agent_a_pr_type_classification")
    guardrails.assert_allowed("agent_b_item_extraction")
    with pytest.raises(ValueError):
        guardrails.assert_allowed("budget_validation")


def test_make_trace_builds_schema():
    t = make_trace(
        source_agent="Agent A", fallback_type="pr_type_classification",
        used=True, reason="metadata insufficient", confidence=0.7,
        normalized_candidate="emergency", model="gpt-x", prompt_version="v1",
        original_evidence="context_packet.json",
    )
    assert isinstance(t, LLMFallbackTrace)
    assert t.used is True and t.normalized_candidate == "emergency"
