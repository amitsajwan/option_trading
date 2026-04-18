"""Integration guide: Using velocity features to clean up deterministic strategy logic.

Quick Summary:
  Velocity features capture the morning session (10:00-11:30) as a deterministic
  fingerprint. We can use these pre-computed features to:
  
  1. Gate entry decisions on morning setup quality (before 12:00)
  2. Reduce reliance on reactive post-11:30 indicators
  3. Make regime classification more confident
  4. Simplify the overall logic

Architecture:

  Before (Reactive):
    10:00-11:30 → accumulate data (unused for entries)
    12:00 → check 5m/15m returns, regime, liquidity → decide

  After (Planned):
    10:00-11:30 → compute velocity features
    11:30 → inject velocity into snapshot
    12:00 → use velocity context to gate entry confidence upfront
           → simpler, more deterministic, fewer surprises


USAGE OPTIONS
=============

Option 1: Drop-In Replacement (Easiest)
  
  Replace the strategy_app engines:

    from strategy_app.engines.velocity_regime_classifier import VelocityEnhancedRegimeClassifier
    from strategy_app.engines.velocity_entry_policy import VelocityEnhancedEntryPolicy

    # In DeterministicRuleEngine.__init__:
    self._regime = VelocityEnhancedRegimeClassifier(model_path=model_path)
    self._policy = VelocityEnhancedEntryPolicy()  # instead of LongOptionEntryPolicy()

  Effect:
    - Regime classification gains velocity confidence boosts
    - Entry policy gains morning_momentum and iv_quality gates upfront
    - All other logic unchanged (backward compatible)


Option 2: Opt-In Configuration (More Flexible)

  In .env.compose or strategy_app command:

    STRATEGY_ENHANCED_VELOCITY=1

  Then in main.py:

    if os.getenv("STRATEGY_ENHANCED_VELOCITY") == "1":
        regime = VelocityEnhancedRegimeClassifier(...)
        policy = VelocityEnhancedEntryPolicy(...)
    else:
        regime = RegimeClassifier(...)
        policy = LongOptionEntryPolicy(...)


WHAT CHANGES?
=============

Regime Classification:
  • Adds velocity_score to evidence (for observability)
  • Boosts confidence for TRENDING if morning momentum + no reversal
  • Validates SIDEWAYS if weak momentum + large range
  • Example: TRENDING with 70% base confidence → 78% with velocity boost

Entry Policy:
  Before:
    [volume] → [option_liquidity] → [momentum] → [timing] → [premium] → [regime]
              (6 gates, all post-11:30)

  After:
    [morning_momentum] + [iv_quality]  (new pre-entry gates using velocity)
    ↓
    [volume] → [option_liquidity] → [momentum] → [timing] → [premium] → [regime]
              (original 6 gates, enhanced with velocity context)

  Morning Momentum Gate:
    BLOCK:   vote opposes morning trend (e.g., voting CE when morning was -1% down)
    PASS:    vote aligns with morning trend (e.g., voting CE when morning was trend=1)
             score boost = trend_strength (0-0.15 typically)
    WARN:    flat morning but weak momentum in vote direction
             score penalty = -0.05

  IV Quality Gate:
    PASS:    volume_spike > 1.2 (elevated liquidity)
             + if IV compressing (better for long premium)
    WARN:    volume_spike < 0.85 (sparse)
    BLOCK:   volume_spike > 0.85 but IV expanding fast (worst case)


WHEN TO USE VELOCITY-ENHANCED VS PLAIN
======================================

Use VelocityEnhanced:
  ✅ Training data has velocity (stage*_view_v2 available)
  ✅ Live snapshot_app has --parquet-root (velocity context loaded)
  ✅ Want to reduce false entries by checking morning setup first
  ✅ Backtesting on historical snapshots with velocity

Use Plain (LongOptionEntryPolicy + RegimeClassifier):
  ✅ Live snapshot_app running without --parquet-root (degraded velocity)
  ✅ Backtesting on old historical data (no velocity columns)
  ✅ Debugging issues (simpler logic path)


OBSERVABILITY & DEBUGGING
=========================

Entry decision now includes:
  checks = {
    "morning_momentum": "PASS:aligned_with_trend dir=CE strength=0.045",
    "iv_quality": "PASS:volume_elevated vol_spike=1.28 + iv_compressing",
    "volume": "PASS:vol_ratio=1.52 strong",
    ...
  }

Regime signal now includes in evidence:
  {
    "morning_momentum_strength": 0.45,
    "morning_reversal_flag": False,
    "morning_range_size": 0.015,
    "oi_buildup_asymmetry": 0.62,
    "morning_trend_direction": 1,  # uptrend
    "vol_spike_today": 1.28,
  }

Use these to understand why entries were allowed/blocked and validate
the velocity context is being read correctly.


TESTING
=======

Before deploying, verify:

1. Velocity is present in live snapshots:
     python -m snapshot_app.health --events-path .run/snapshot_app/events.jsonl
     # Look for velocity_enrichment block in 11:30+ snapshots

2. Velocity reading works in strategy accessor:
     snap = SnapshotAccessor(snapshot_dict)
     assert snap.has_velocity
     print(snap.vel("vel_price_delta_open"))  # Should not be NaN

3. Enhanced policy evaluates correctly:
     from strategy_app.engines.velocity_entry_policy import VelocityEnhancedEntryPolicy
     policy = VelocityEnhancedEntryPolicy()
     decision = policy.evaluate(snap, vote, regime, risk)
     print(decision.checks)  # Should include morning_momentum gate


RECOMMENDED ROLLOUT
===================

Phase 1 (Research/Offline):
  • Generate training data with velocity (done: 1199 days staged)
  • Test VelocityEnhancedEntryPolicy in backtests
  • Compare vs plain LongOptionEntryPolicy on historical returns
  • Adjust PolicyConfig thresholds if needed

Phase 2 (Paper Trading):
  • Enable STRATEGY_ENHANCED_VELOCITY=1 in docker-compose
  • Run deterministic engine in paper mode
  • Observe entry_policy.checks logs for gating behavior
  • Validate false entry reduction (morning momentum gate working)

Phase 3 (Shadow):
  • Run with real market data (no real trades)
  • Collect 10+ days of entry decisions
  • Compare vs non-velocity baseline
  • Finalize thresholds

Phase 4 (Capped Live):
  • Deploy with STRATEGY_ENHANCED_VELOCITY=1
  • Monitor entry rate and P&L
  • Compare to past deterministic-only performance


FILES MODIFIED
==============

New files (add to strategy_app package):
  • strategy_app/engines/velocity_regime_classifier.py
  • strategy_app/engines/velocity_entry_policy.py

To integrate:
  • Update strategy_app/engines/__init__.py to export them
  • Optionally add STRATEGY_ENHANCED_VELOCITY env var to main.py
  • Wire into DeterministicRuleEngine instantiation
"""
