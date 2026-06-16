"""Tests for the LLM direction advisor, grounding cache, and shadow recorder.

No network: the LLM transport (chat_completion) and gemini_web fetch are monkeypatched.
"""
from __future__ import annotations

import json

import pytest

from strategy_app.brain import direction_advisor as da
from strategy_app.brain.providers.openai_compatible import extract_json_object


# ── extract_json_object: the trailing-brace bug that broke flash-lite parsing ──────
def test_extract_json_handles_trailing_junk_brace():
    # flash-lite appends a stray "}\n}" after a valid object — must still parse.
    raw = '{"direction":"CE","confidence":0.7,"reason":"up"}\n}'
    assert extract_json_object(raw) == {"direction": "CE", "confidence": 0.7, "reason": "up"}


def test_extract_json_handles_fenced_and_prose():
    raw = "Here you go:\n```json\n{\"direction\": \"PE\", \"confidence\": 0.6}\n```"
    assert extract_json_object(raw)["direction"] == "PE"


# ── build_facts (parquet row) ──────────────────────────────────────────────────────
def test_build_facts_curates_and_skips_nan_and_missing():
    row = {"ret_5m": 0.0021, "vwap_distance": -0.0014, "osc_rsi_14": 38.2,
           "adx_14": float("nan"), "px_fut_close": 52000.0}  # px not in map → skipped
    facts = da.build_facts(row)
    assert facts["return_5m"] == 0.0021
    assert facts["price_vs_vwap_frac"] == -0.0014
    assert facts["rsi_14"] == 38.2
    assert "adx_14" not in facts          # NaN dropped
    assert "px_fut_close" not in facts    # not in FACT_COLUMNS


# ── build_facts_from_accessor (live snapshot duck-typing) ──────────────────────────
class _FakeSnap:
    fut_return_5m = 0.0018
    price_vs_vwap = 0.0009
    ema_9 = 100.0
    ema_21 = 98.0
    atm_ce_iv = 12.0
    atm_pe_iv = 13.2
    pcr = 0.85
    orh_broken = True
    orl_broken = False
    fut_close = 52010.0
    timestamp = "2026-06-10T10:00:00"


def test_build_facts_from_accessor_maps_and_derives():
    facts = da.build_facts_from_accessor(_FakeSnap())
    assert facts["return_5m"] == 0.0018
    assert facts["ema9_minus_ema21"] == 2.0           # derived
    assert round(facts["iv_skew_pe_over_ce"], 3) == 1.1  # 13.2/12.0
    assert facts["orb_high_broken"] == 1
    assert facts["orb_low_broken"] == 0


# ── ask_direction: success, bad-direction coercion, no-key, transport error ────────
def _patch_chat(monkeypatch, content=None, exc=None):
    def fake(**kwargs):
        if exc is not None:
            raise exc
        return content
    monkeypatch.setattr(da, "chat_completion", fake)


def test_ask_direction_success(monkeypatch):
    _patch_chat(monkeypatch, content='{"direction":"PE","confidence":0.72,"reason":"below vwap"}')
    v = da.ask_direction({"return_5m": -0.002}, base_url="u", api_key="k", model="m")
    assert v.direction == "PE" and v.confidence == 0.72 and v.committed


def test_ask_direction_coerces_unknown_to_abstain(monkeypatch):
    _patch_chat(monkeypatch, content='{"direction":"maybe","confidence":2}')
    v = da.ask_direction({"x": 1}, base_url="u", api_key="k", model="m")
    assert v.direction == "ABSTAIN" and not v.committed
    assert 0.0 <= v.confidence <= 1.0


def test_ask_direction_no_key_returns_abstain():
    v = da.ask_direction({"x": 1}, base_url="u", api_key="", model="m")
    assert v.direction == "ABSTAIN" and v.error


def test_ask_direction_transport_error_is_abstain(monkeypatch):
    from strategy_app.brain.providers.openai_compatible import LLMClientError
    _patch_chat(monkeypatch, exc=LLMClientError("HTTP 500 boom"))
    v = da.ask_direction({"x": 1}, base_url="u", api_key="k", model="m", max_retries=0)
    assert v.direction == "ABSTAIN" and "500" in v.error


def test_ask_direction_includes_web_context(monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured["messages"] = kwargs["messages"]
        return '{"direction":"CE","confidence":0.6,"reason":"x"}'
    monkeypatch.setattr(da, "chat_completion", fake)
    v = da.ask_direction({"return_5m": 0.001}, base_url="u", api_key="k", model="m",
                         web_context="RBI policy today; global risk-off")
    assert v.grounded
    assert "web_context" in captured["messages"][1]["content"]
    assert "RBI policy" in captured["messages"][1]["content"]


# ── GeminiGrounding TTL cache ──────────────────────────────────────────────────────
def test_grounding_disabled_returns_empty():
    from strategy_app.brain.gemini_grounding import GeminiGrounding
    g = GeminiGrounding(api_key="", enabled=True)  # no key → not enabled
    assert g.enabled is False
    assert g.get() == ""


def test_grounding_caches_within_ttl(monkeypatch):
    import strategy_app.brain.gemini_grounding as gg
    calls = {"n": 0}

    def fake_fetch(**kwargs):
        calls["n"] += 1
        return f"context-{calls['n']}"
    monkeypatch.setattr(gg, "fetch_web_context", fake_fetch)
    g = gg.GeminiGrounding(api_key="k", ttl_seconds=10_000, enabled=True)
    assert g.get() == "context-1"
    assert g.get() == "context-1"   # cached, no second fetch
    assert calls["n"] == 1


def test_grounding_serves_stale_on_failed_refresh(monkeypatch):
    import strategy_app.brain.gemini_grounding as gg
    seq = ["good", ""]  # first ok, then refresh fails

    def fake_fetch(**kwargs):
        return seq.pop(0)
    monkeypatch.setattr(gg, "fetch_web_context", fake_fetch)
    g = gg.GeminiGrounding(api_key="k", ttl_seconds=0, enabled=True)  # ttl 0 → always refetch
    assert g.get() == "good"
    assert g.get() == "good"   # refresh returned "" → serve last-good, not blank


# ── DirectionShadow recorder (synchronous write, no network) ───────────────────────
def test_shadow_disabled_is_noop(tmp_path):
    from strategy_app.brain.direction_shadow import DirectionShadow
    sh = DirectionShadow(enabled=False, out_path=tmp_path / "s.jsonl")
    sh.record(_FakeSnap(), object())
    assert not (tmp_path / "s.jsonl").exists()


def test_shadow_writes_line(monkeypatch, tmp_path):
    import strategy_app.brain.direction_shadow as ds

    # make the call synchronous + deterministic
    monkeypatch.setattr(ds, "ask_direction", lambda *a, **k: da.DirectionVerdict(
        "CE", 0.66, reason="r", model="groq", grounded=False))

    class _Taken:
        class _D:
            value = "PE"
        direction = _D()
        source = "composite(vwap:PE)"
        ce_score = 1.0
        pe_score = 2.0

    sh = ds.DirectionShadow(enabled=True, provider="groq", api_key="k",
                            out_path=tmp_path / "s.jsonl")
    # force-enable bypassing the base_url/key gate is unnecessary: provider groq resolves
    assert sh.enabled
    sh._call_and_write(da.build_facts_from_accessor(_FakeSnap()),
                       {"taken_direction": "PE", "ts": "t", "ce_score": 1.0, "pe_score": 2.0})
    lines = (tmp_path / "s.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["llm_direction"] == "CE"
    assert rec["taken_direction"] == "PE"
    assert rec["agrees_taken"] is False


def test_summarize_shadow_log(tmp_path):
    from strategy_app.brain.direction_shadow import summarize_shadow_log
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"llm_direction": "CE", "llm_confidence": 0.7, "agrees_taken": True, "truth": "CE"},
        {"llm_direction": "PE", "llm_confidence": 0.6, "agrees_taken": False, "truth": "CE"},
        {"llm_direction": "ABSTAIN", "llm_confidence": 0.0},
        {"llm_direction": "ABSTAIN", "llm_error": "HTTP 429"},
    ]) + "\n")
    s = summarize_shadow_log(p)
    assert s["n"] == 4
    assert s["n_committed"] == 2
    assert s["n_errors"] == 1
    assert s["n_abstain"] == 1
    assert s["agree_with_taken_rate"] == 0.5
    assert s["llm_accuracy_if_truthed"] == 0.5  # CE correct, PE wrong vs truth CE
