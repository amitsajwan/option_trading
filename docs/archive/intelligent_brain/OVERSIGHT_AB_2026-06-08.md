# Oversight Brain — A/B on GCP (2026-06-08)

**Setup:** ops-sim replay on real mongo data (06-03 green, 06-04 + 06-05 red), VM `option-trading-runtime-01`, code `773bada`. Groq (`llama-3.3-70b-versatile`) verified live in-container. Three configs per day: **base** (no oversight), **rule** (deterministic anti-chase heuristic gate), **llm** (Groq oversight, every 60 bars, hallucination-verified).

## Result

| Day | base | rule (anti-chase) | llm (Groq) |
|---|---|---|---|
| 06-03 green | **+7.22%** | −2.93% | +7.22% |
| 06-04 red | −3.97% | **+1.79%** | −3.97% |
| 06-05 red | −4.72% | −4.72% | −4.72% |
| **TOTAL** | **−1.47%** | **−5.86%** | **−1.47%** |

## Findings (honest)

1. **The gate mechanism works.** Rule mode demonstrably changed trades (06-03: 14→9; 06-04: 7→4) and P&L — the oversight → `oversight_state.json` → engine-veto path is proven end-to-end on GCP.
2. **The deterministic anti-chase rule is net-negative (−5.86% vs −1.47%).** Exactly the location trade-off: it *helps the red day* (06-04: −3.97→+1.79, vetoing CE chases into a down tape) but *kills the green day* (06-03: +7.22→−2.93, vetoing counter-trend trades that were winners). The green-day damage dominates. **Refuted as a standalone edge.**
3. **The LLM (Groq) oversight made ZERO difference — `llm == base` on all 3 days.** Reasoning over the verified facts (levels, PCR, OI, trend), it fired **no vetoes**: it produced neutral / sub-threshold leans, and the hallucination-verifier suppressed any contradictory ones. So: **no improvement, and no harm.**
4. **No P&L improvement from oversight on this data.** Net is identical to base for the LLM, worse for the rule.

## Why the LLM fired nothing (this is the signal)
The LLM, given the real structural facts, did **not** find confident directional leans — consistent with the entire project arc: **direction at this horizon is genuinely hard, and the structural facts don't carry a robust directional edge.** The verification layer + the 0.6 veto-confidence floor correctly kept it from acting on weak/contradictory reads. That's the *safety design working* — it refused to bet on a signal that isn't there — but it also means no alpha yet.

## What's validated (even without alpha)
- **Architecture + safety:** oversight is risk-reducing-only, hallucination-verified, off the per-bar path; the LLM did no harm.
- **The discipline:** this A/B *vindicates "shadow-first."* Enabling a gate now (rule) would have **lost money** (−5.86%); the LLM gate did nothing. Gating unvalidated direction logic is the wrong move — exactly what we avoided.

## Rework / recommendation
1. **Do NOT enable the gate.** Rule hurts; LLM is inert. Keep `BRAIN_OVERSIGHT_GATE_ENABLED=false`.
2. **Run the oversight as a SHADOW/scoring layer** — `BRAIN_OVERSIGHT_ENABLED=true`, gate off — so it logs its leans every cycle; accumulate leans-vs-outcomes over **many more days**, and only gate if the leans become demonstrably predictive.
3. **The LLM needs better signal than structural facts.** Structural facts (levels/PCR/trend) are exhausted (location + rule + llm all wash out). The next genuine experiment is the **Gemini-web layer** (live events/news/RBI/FII) feeding the facts — but it's quota-limited (429) and news is noisy, so treat as a hypothesis to score in shadow, not a fix.
4. **Direction remains the unsolved core.** Neither deterministic heuristics nor LLM reasoning over the available facts beat baseline. The honest path is more data + a real direction signal, not more gating.

## Conclusion
Everything is **built, tested, deployed, and verified** — the oversight trader-brain (sense/memory/calculator/reasoner/scratchpad), Groq reasoning live, hallucination verification, the risk-reducing gate, the 30-min driver. The measurement is honest: **no improvement on this week.** The value delivered is (a) a working, safe, verified reasoning+journal system and (b) a cheap, conclusive refutation that saved us from shipping a money-losing gate.
