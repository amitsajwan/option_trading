# Layer-3 LLM Oversight — Free Model Options & Wiring

**Date:** 2026-06-07 · **Status:** implemented, OFF by default (`BRAIN_LLM_ENABLED=false`) · **Companion:** [INTELLIGENT_BRAIN_HANDOVER.md](INTELLIGENT_BRAIN_HANDOVER.md) §7, Decision D6

The LLM lives **only** at Layer 3 (oversight), runs **once per day** at morning
briefing, is **off the per-bar hot path**, and is **advisory** — the brain treats
`llm.day_assessment` as one hint among several and never lets it place or size a
trade. This doc records the free-model comparison and how to switch providers.

## Why provider choice is low-stakes here

The provider is hit ~1×/day with a few-thousand-token prompt. Latency (seconds)
and cost (effectively zero) don't matter — that's the whole reason the design
quarantines the LLM to Layer 3. The implementation targets the **OpenAI-compatible
`/chat/completions`** shape, so switching providers is **config, not code**: set
`BRAIN_LLM_BASE_URL` + `BRAIN_LLM_MODEL` + `BRAIN_LLM_API_KEY`.

## Recommended free models

Ranked for *this* task (classify the day into CALM/NEUTRAL/VOLATILE/AVOID with a
one-line rationale from small quantitative context). All are genuinely free at our
volume. **Confirm the exact model slug against each provider's current model list
at integration time — slugs change.**

| Rank | Provider | `BRAIN_LLM_BASE_URL` | Suggested `BRAIN_LLM_MODEL` | Why / caveat |
|---|---|---|---|---|
| **1** | **Groq** | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | Free tier, very fast, 70B quality is ample for a daily classification. JSON mode supported. **Default.** |
| **2** | **Google Gemini** | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.0-flash` | Genuinely free tier, generous daily quota, reliable. OpenAI-compat endpoint. |
| **3** | **DeepSeek** | `https://api.deepseek.com` | `deepseek-chat` (V3) or `deepseek-reasoner` (R1) | Strongest *reasoning* of the set; near-free. **Caveat: official API stores data in China** — avoid if trace privacy matters. |
| 4 | **xAI Grok** | `https://api.x.ai/v1` | `grok-3-mini` | Periodic free credits (not permanently free); fine quality. |
| 5 | **OpenRouter** | `https://openrouter.ai/api/v1` | a `…:free` variant | Aggregator; tight rate limits + variable availability, but a good fallback. |

**Pick:** start with **Groq** (default, free + fast). If you'd rather not create a
Groq account, **Gemini Flash** is the next-cleanest free tier. Keep **DeepSeek-R1**
in your pocket for higher-quality EOD reasoning *if* China data-residency is
acceptable. If you want everything on your own box (no data leaves the VM), the
local **Ollama** path (Q4 7–14B on the current CPU VM, once/day) also speaks the
OpenAI-compatible shape — point `BRAIN_LLM_BASE_URL` at `http://localhost:11434/v1`.

## How to turn it on (integration)

The code is implemented and unit-tested; it's a no-op until you set env vars:

```bash
# .env.compose (or wherever strategy_app reads env)
BRAIN_LLM_ENABLED=true
BRAIN_LLM_API_KEY=<your key>
BRAIN_LLM_BASE_URL=https://api.groq.com/openai/v1
BRAIN_LLM_MODEL=llama-3.3-70b-versatile
# optional tuning:
# BRAIN_LLM_TIMEOUT_S=20
# BRAIN_LLM_MAX_TOKENS=512
# BRAIN_LLM_TEMPERATURE=0.2
# BRAIN_LLM_JSON_MODE=true        # set false for providers that reject response_format
# BRAIN_LLM_FEATURES_PATH=/path/to/daily_regime_features.json   # else DailyFeatures default
```

No rebuild needed if env is bind-mounted — restart strategy_app. Verify in the
startup/morning-briefing log: `llm_context date=… model=… assessment=…`.

## What it sends and returns

- **Input:** today's date + the rolling daily regime features (rv20, VIX, 60-day
  return, SMA slope) read from the same `daily_regime_features.json` the
  `DailyFeaturesProvider` uses — so the call is *grounded*, not a blind guess.
- **Output** (merged into `DayContext.provider_context`):
  - `llm.day_assessment` ∈ `CALM|NEUTRAL|VOLATILE|AVOID` (anything else is dropped)
  - `llm.confidence` (0..1), `llm.reasoning`, `llm.risk_notes`, `llm.model`
- The brain consumes `llm.day_assessment` in `_synthesise_day_score` as the
  highest-priority hint when present (after the hard 3-losing-day avoid).

## Safety properties (by construction)

- **Never raises** — any transport/timeout/parse failure → `{}`, brain falls back
  to deterministic daily-feature scoring.
- **Bounded** by `BRAIN_LLM_TIMEOUT_S` so a slow free endpoint can't stall briefing.
- **Validated** — only the four known assessment labels reach the brain; malformed
  JSON from small models is tolerated (fenced / prose-wrapped) or discarded.
- **No new dependency** — the client uses the Python stdlib (`urllib`), not `requests`.

## Files

- [strategy_app/brain/providers/openai_compatible.py](../strategy_app/brain/providers/openai_compatible.py) — zero-dep client + JSON extraction
- [strategy_app/brain/providers/llm_stub.py](../strategy_app/brain/providers/llm_stub.py) — `LLMContextProvider` (the provider)
- [strategy_app/tests/test_llm_context_provider.py](../strategy_app/tests/test_llm_context_provider.py) — 12 tests, no network
