# Architecture Review AR8 (Post T33)

Date: `2026-02-22`  
Scope checkpoint: after T33 (order intent + reconciliation + runtime guards)

## Reviewed Areas

1. Order-intent idempotency and replay safety
2. Decision-vs-fill reconciliation integrity
3. Runtime guardrails and kill-switch behavior

## Findings

1. T33 runtime establishes a concrete idempotent contract:
   - deterministic `intent_id`
   - normalized intent/fill tables
   - duplicate suppression.
2. Real-stream reconciliation from `t33_paper_capital_events_actual.jsonl` is internally consistent:
   - intents (raw/deduped): `126/103`
   - fills (raw/deduped): `126/103`
   - matched intents: `103`
   - unmatched intents/fills: `0/0`
   - side/kind mismatches: `0/0`.
3. Runtime guards are enforceable and currently active:
   - `kill_switch=true`
   - max consecutive losses: `20` (threshold `4`)
   - max drawdown: `-1.0` (threshold `-0.3`).
4. Guard output is operationally explicit (`halt` status + typed alerts), suitable for promotion gating.

## Decisions

1. Keep `kill_switch=true` as hard `NO-GO` for live promotion.
2. Treat reconciliation parity as necessary but not sufficient; guard status must also pass.
3. Keep intent/fill parquet outputs mandatory for every paper/shadow run.

## Refactor Actions

No blocking refactor required at AR8 gate.

Accepted follow-up improvements:

1. Add explicit monotonic source sequence field into intent contract (for upstream ordering diagnostics).
2. Add optional broker-fill adapter mode as preferred source over decision-derived fills.
3. Add guard cool-down/manual-ack workflow to avoid accidental auto-resume after halt.

## Risks for AR9

1. Current default fill-source fallback can overestimate reconciliation quality when broker fills are unavailable.
2. Drawdown guard depends on return-path assumptions; calibration to lot-level PnL should be verified before live cutover.
3. Halt conditions are triggered on current stream; model/exit policy still not operationally safe for live capital.
