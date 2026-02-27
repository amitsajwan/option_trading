# Architecture Review AR5 (Post T21)

Date: `2026-02-22`  
Scope checkpoint: after T21 (paper replay evaluation harness)

## Reviewed Areas

1. Replay-to-backtest parity assumptions
2. Timestamp alignment and unmatched-decision handling
3. Execution realism observability for paper decisions

## Findings

1. Replay evaluation now maps T11 decisions to labeled timestamps and computes realized outcomes with selected execution profile.
2. Matching logic is explicit and measurable:
   - `matched_trades`
   - `unmatched_buy_decisions`
   - `match_rate`
3. Outcome pricing shares the same core execution path as backtest (`compute_trade_outcome_from_row`), reducing divergence risk.
4. Fill model integration is now consistently available in both backtest and replay evaluation paths.

## Decisions

1. Keep strict timestamp-join behavior (no fuzzy nearest-minute matching) for auditability.
2. Preserve unmatched decision reporting as a first-class operational metric.
3. Use T19 best profile as default replay-evaluation execution profile when available.

## Refactor Actions

No blocking refactor required at AR5 gate.

Accepted follow-ups for T22/T23:

1. Add explicit replay watermark/freshness tracking in live adapter.
2. Add per-profile drift segmentation for exit reasons and hold duration.
3. Add optional nearest-bar fallback mode only if explicitly enabled and logged.

## Risks for T22+

1. Realtime data latency may increase unmatched decisions if timestamp normalization differs across systems.
2. Replay evaluation assumes same symbol mapping contract as label generation; schema drift can break joins.
3. Exit profile changes without synchronized reporting can cause interpretability gaps.
