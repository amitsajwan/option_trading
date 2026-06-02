---
name: project-depth-plugin-state
description: Depth Plugin stage (Stage 4) added to the 7-stage pipeline as of 2026-05-31
metadata:
  type: project
---

Depth Plugin inserted between Direction and Strike in the pipeline.

## Updated pipeline (7 stages)
```
Market Snapshot → Regime → Entry → Direction → Depth → Strike → Risk → Execution
```

## Depth Plugin design (user-specified)
- Acts as confidence modifier, not just pass/fail
- CE trade + strong CE bid → confidence boost (+DEPTH_ALIGN_BOOST, default +0.05)
- CE trade + heavy sell pressure → confidence reduction (+DEPTH_OPPOSE_PENALTY, default -0.10)
- When depth absent (replay/feed offline): always proceed=True, confidence unchanged
- DEPTH_HARD_GATE=1 enables hard rejection (default: advisory/soft gate)

## Files created
- `strategy_app/market/depth_plugin.py` — PassthroughDepthPlugin, LiveDepthPlugin, resolve_depth_plugin()
- `strategy_app/consumers/depth_decision_consumer.py` — DepthDecisionConsumer
- `strategy_app/tests/test_depth_stage.py` — 39 tests

## Files modified
- `contracts_app/decision_events.py` — Added DepthDecisionEvent (with confidence, ce_bid_strength, pe_bid_strength, depth_aligned, spread_pct)
- `contracts_app/sim_namespace.py` — Added "depth_decisions" to _NAMESPACED_BASES
- `contracts_app/topics.py` — Added depth_decisions_topic()
- `strategy_app/brain/plugin.py` — Added DepthPlugin ABC + DepthDecisionResult NamedTuple
- `strategy_app/consumers/strike_decision_consumer.py` — Now reads from depth_decisions (not direction_decisions); uses depth-adjusted confidence
- `strategy_app/consumers/__init__.py` — Exports DepthDecisionConsumer
- `contracts_app/__init__.py` — Exports new depth symbols

## Env vars
- DEPTH_FEED_ENABLED=1 → activates LiveDepthPlugin (default: PassthroughDepthPlugin)
- DEPTH_HARD_GATE=1 → hard block when depth strongly opposes
- DEPTH_MAX_SPREAD_PCT → spread threshold (default 0.02)
- DEPTH_ALIGN_BOOST → confidence boost when aligned (default +0.05)
- DEPTH_OPPOSE_PENALTY → confidence reduction when opposed (default -0.10)
- DEPTH_HARD_BLOCK_THRESHOLD → minimum confidence for hard gate (default 0.30)

## Why: user rationale
Depth is useful for BankNifty but not sufficient alone. Relative predictive value:
Regime > Price structure > VWAP > ATR > Volume accel > Option premium > OI > Option depth > Raw L2

Depth enables A/B experimentation: Direction-only vs Direction+Depth vs Direction+Depth+Regime.
