"""Unit tests for the LLM oversight provider (no network).

Covers: disabled/guard behaviour, JSON extraction robustness, response
normalisation/validation, and the full provide() path with a patched client.
"""

from __future__ import annotations

from datetime import date

import pytest

from strategy_app.brain.providers import llm_stub
from strategy_app.brain.providers.llm_stub import LLMContextProvider
from strategy_app.brain.providers.openai_compatible import (
    LLMClientError,
    extract_json_object,
)

_ENV_VARS = [
    "BRAIN_LLM_ENABLED",
    "BRAIN_LLM_API_KEY",
    "BRAIN_LLM_BASE_URL",
    "BRAIN_LLM_MODEL",
    "BRAIN_LLM_FEATURES_PATH",
    "BRAIN_LLM_JSON_MODE",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


# ─────────────────────────── guard behaviour ────────────────────────────────

class TestGuards:
    def test_disabled_returns_empty(self):
        assert LLMContextProvider().provide(date(2024, 5, 15)) == {}

    def test_enabled_without_key_returns_empty(self, monkeypatch):
        monkeypatch.setenv("BRAIN_LLM_ENABLED", "true")
        # no API key set
        assert LLMContextProvider().provide(date(2024, 5, 15)) == {}

    def test_client_failure_degrades_to_empty(self, monkeypatch):
        monkeypatch.setenv("BRAIN_LLM_ENABLED", "true")
        monkeypatch.setenv("BRAIN_LLM_API_KEY", "k")

        def _boom(**_kwargs):
            raise LLMClientError("network down")

        monkeypatch.setattr(llm_stub, "chat_completion", _boom)
        assert LLMContextProvider().provide(date(2024, 5, 15)) == {}


# ─────────────────────────── JSON extraction ────────────────────────────────

class TestExtractJsonObject:
    def test_direct(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        content = "```json\n{\"day_assessment\": \"CALM\"}\n```"
        assert extract_json_object(content) == {"day_assessment": "CALM"}

    def test_prose_wrapped(self):
        content = 'Here is my call:\n{"day_assessment": "AVOID"}\nThanks!'
        assert extract_json_object(content) == {"day_assessment": "AVOID"}

    def test_no_object_raises(self):
        with pytest.raises(LLMClientError):
            extract_json_object("sorry, I cannot help with that")


# ─────────────────────────── normalisation ──────────────────────────────────

class TestNormalise:
    def test_valid_full(self):
        out = LLMContextProvider._normalise(
            {
                "day_assessment": "calm",
                "confidence": 0.8,
                "reasoning": "low vol",
                "risk_notes": "watch open",
            }
        )
        assert out["llm.day_assessment"] == "CALM"
        assert out["llm.confidence"] == 0.8
        assert out["llm.reasoning"] == "low vol"
        assert out["llm.risk_notes"] == "watch open"

    def test_invalid_assessment_dropped(self):
        out = LLMContextProvider._normalise({"day_assessment": "BULLISH"})
        assert "llm.day_assessment" not in out

    def test_confidence_clamped_and_bool_rejected(self):
        assert LLMContextProvider._normalise({"confidence": 1.7})["llm.confidence"] == 1.0
        assert LLMContextProvider._normalise({"confidence": -3})["llm.confidence"] == 0.0
        assert "llm.confidence" not in LLMContextProvider._normalise({"confidence": True})

    def test_empty_object(self):
        assert LLMContextProvider._normalise({}) == {}

    def test_week_summary_and_news_mapped(self):
        out = LLMContextProvider._normalise(
            {
                "day_assessment": "NEUTRAL",
                "week_summary": "down ~0.5% on the week, range-bound",
                "news": "unknown",
            }
        )
        assert out["llm.week_summary"].startswith("down")
        assert out["llm.news"] == "unknown"


# ─────────────────────────── full provide() path ────────────────────────────

class TestProvidePath:
    def test_maps_response_to_keys(self, monkeypatch):
        monkeypatch.setenv("BRAIN_LLM_ENABLED", "true")
        monkeypatch.setenv("BRAIN_LLM_API_KEY", "k")
        monkeypatch.setenv("BRAIN_LLM_MODEL", "test-model")

        captured = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return '{"day_assessment": "VOLATILE", "confidence": 0.6, "reasoning": "wide range"}'

        monkeypatch.setattr(llm_stub, "chat_completion", _fake)

        out = LLMContextProvider().provide(date(2024, 5, 15))
        assert out["llm.day_assessment"] == "VOLATILE"
        assert out["llm.confidence"] == 0.6
        assert out["llm.reasoning"] == "wide range"
        assert out["llm.model"] == "test-model"
        # verb sanity: the model + a system/user message pair were sent
        assert captured["model"] == "test-model"
        assert [m["role"] for m in captured["messages"]] == ["system", "user"]

    def test_unparseable_response_degrades_to_empty(self, monkeypatch):
        monkeypatch.setenv("BRAIN_LLM_ENABLED", "true")
        monkeypatch.setenv("BRAIN_LLM_API_KEY", "k")
        monkeypatch.setattr(
            llm_stub, "chat_completion", lambda **_k: "no json here at all"
        )
        assert LLMContextProvider().provide(date(2024, 5, 15)) == {}
