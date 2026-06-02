# Scrum Backlog: Live Trading
*Sprint-ready stories with Definition of Done. Owner: Engineering Team.*

---

## Definition of Done (applies to every story)

A story is Done when ALL of the following are true:

1. **Code merged** to `mordenization` branch, reviewed by ≥1 engineer
2. **Tests pass** — unit tests for new logic, existing tests not broken
3. **Deployed** to `option-trading-runtime-01` via `docker compose up -d`
4. **Trace verified** — decision trace or dashboard confirms the change behaves as designed for at least one live/sim snapshot
5. **No regressions** — dashboard shows correct data, no new "Feed offline" or wrong values
6. **Documented** — one-paragraph update to the relevant doc in `docs/`

---

## Epics

| # | Epic | Theme | Priority |
|---|---|---|---|
| E1 | Execution Bridge | Go-live prerequisite | P0 |
| E2 | Exit Strategy | P&L improvement (biggest gap) | P0 |
| E3 | Entry Gate Hardening | Trade quality | P1 |
| E4 | Risk Sizing | Capital management | P1 |
| E5 | Observability & Alerts | Operator confidence | P1 |
| E6 | Data Quality Fixes | System integrity | P2 |
| E7 | Backtest Validation | Strategy confidence | P2 |

---

## E1 — Execution Bridge
*Nothing goes live without this. Estimated: 8–10 days.*

---

### E1-S1: BrokerAdapter interface + PaperAdapter
**Points:** 3

**Story:** As an engineer, I want a clean broker abstraction so the system can switch between paper and live execution without changing strategy code.

**Acceptance Criteria:**
- `execution_app/adapter/base.py` defines `BrokerAdapter` ABC with `place_entry`, `place_exit`, `get_order_status`, `cancel_order`
- `OrderResult` dataclass: `order_id`, `status`, `fill_price`, `fill_qty`, `error`
- `PaperAdapter` implements `BrokerAdapter` — returns immediate fill at `signal.entry_premium`
- `PaperAdapter` behaviour is identical to current simulation (same P&L as today's paper trades)
- `EXECUTION_ADAPTER=paper` env var selects `PaperAdapter`

**DoD extras:**
- Unit test: `PaperAdapter.place_entry()` returns `status=filled` with correct price
- Unit test: `PaperAdapter.place_exit()` returns `status=filled`

---

### E1-S2: KiteAdapter — place order + poll fill
**Points:** 5

**Story:** As an engineer, I want `KiteAdapter` to place real Kite NFO orders and confirm fills so live P&L is based on actual execution.

**Acceptance Criteria:**
- `KiteAdapter.place_entry(signal)` calls `kite.place_order()` with correct NFO tradingsymbol, BUY, MARKET, NRML
- Tradingsymbol built correctly from `signal.strike`, `signal.expiry`, `signal.direction` for BANKNIFTY weekly options
- `KiteAdapter.place_exit(signal, position)` calls `kite.place_order()` with SELL
- `KiteAdapter.get_order_status(order_id)` returns fill price and qty when status=COMPLETE
- Order tagged with `signal.signal_id[:10]` for Kite audit trail
- `EXECUTION_ADAPTER=kite` env var selects `KiteAdapter`

**DoD extras:**
- Integration test against Kite paper trading API (not live funds)
- `LOT_SIZE_BANKNIFTY=15` constant defined in config (verify against current SEBI contract spec)
- Error path tested: rejected order logs warning and emits `OrderResult(status=rejected)`

---

### E1-S3: execution_app service
**Points:** 3

**Story:** As an operator, I want `execution_app` running as a Docker service that subscribes to trade signals and routes them to the broker adapter.

**Acceptance Criteria:**
- `execution_app/__main__.py` starts service, selects adapter from `EXECUTION_ADAPTER` env
- Subscribes to `trade_signal_topic()` Redis pubsub
- ENTRY signals → `adapter.place_entry()` → emit `FillEvent` to `execution:fills:v1`
- EXIT signals → `adapter.place_exit()` → emit `FillEvent` to `execution:fills:v1`
- Service logs every order placement and fill at INFO level
- Service is in `docker-compose.yml` as `execution_app`, depends on `redis`, `strategy_app`
- `docker compose up -d execution_app` starts without errors

**DoD extras:**
- Health endpoint at `/health` returns `{"status": "ok", "adapter": "paper|kite"}`
- Unit test: ENTRY signal → paper adapter → fill emitted to Redis

---

### E1-S4: Fill tracking + real P&L in MongoDB
**Points:** 5

**Story:** As a trader, I want real fill prices stored so P&L on the dashboard reflects actual execution, not simulated snapshot prices.

**Acceptance Criteria:**
- `fill_tracker.py` consumes `execution:fills:v1` Redis stream
- When fill received for ENTRY: updates `strategy_positions` MongoDB doc with `fill_entry_price`, `fill_order_id`, `fill_timestamp`
- When fill received for EXIT: updates doc with `fill_exit_price`, `fill_pnl_pct` (computed from real fill prices)
- Dashboard `/api/strategy/current/state` prefers `fill_pnl_pct` over simulated `pnl_pct` when available
- Dashboard trade inspector shows `FILL ` badge when real fill price differs from signal price by > 0.1%

**DoD extras:**
- Slippage field: `slippage_pct = fill_price - signal_premium) / signal_premium` stored per trade
- Unit test: fill event → MongoDB update → dashboard API returns real P&L

---

### E1-S5: Shadow mode adapter
**Points:** 5

**Story:** As a trader, I want to run real orders alongside paper simulation for 1 week before committing to full live, so I can measure real slippage without risk.

**Acceptance Criteria:**
- `ShadowAdapter` wraps both `KiteAdapter` (max 1 lot) and `PaperAdapter`
- Both adapters called on every signal; results published separately: `fill:real` and `fill:paper`
- Dashboard shows side-by-side: "Real P&L: +X%" vs "Paper P&L: +Y%"
- Daily slippage report: average `real_fill - paper_fill` across all trades
- `EXECUTION_ADAPTER=shadow` env var selects shadow mode
- `SHADOW_MAX_LOTS=1` caps real Kite orders

**DoD extras:**
- Shadow mode tested for 5 trading days with trader sign-off before E1-S6

---

### E1-S6: Production cutover
**Points:** 2

**Story:** As an operator, I want to switch from shadow to full live by changing one env var.

**Acceptance Criteria:**
- `EXECUTION_ADAPTER=kite` + `size_multiplier=0.25` is the only change needed for full live
- Runbook `docs/runbooks/LIVE_CUTOVER.md` written and reviewed
- Rollback procedure documented: `EXECUTION_ADAPTER=paper` reverts instantly
- 5-trading-day shadow results reviewed and approved by trader before this story starts

---

## E2 — Exit Strategy
*Highest P&L impact. Trade 1 alone left 3.63% on the table.*
*Estimated: 5–6 days.*

---

### E2-S1: ExitPolicy interface
**Points:** 2

**Story:** As an engineer, I want a clean exit policy abstraction so strategies can compose multiple exit conditions without modifying position tracking code.

**Acceptance Criteria:**
- `strategy_app/position/exit_policy.py` defines `ExitPolicy` ABC
- Methods: `check(position, snap) -> Optional[ExitReason]`, `name: str`
- Existing TIME_STOP logic extracted into `TimestopPolicy(max_bars: int)`
- `CompositeExitPolicy(policies: list[ExitPolicy])` — first to trigger wins
- All existing exit behaviour unchanged (no regression)

**DoD extras:**
- Unit test: `CompositeExitPolicy` returns first non-None result
- Unit test: `TimestopPolicy` matches existing TIME_STOP behaviour exactly

---

### E2-S2: PremiumTargetPolicy
**Points:** 2

**Story:** As a trader, I want a profit target that exits when the option premium gains X% from entry, so winners aren't held until they reverse.

**Acceptance Criteria:**
- `PremiumTargetPolicy(target_pct=0.015)` — exit when `position.pnl_pct >= target_pct`
- Configurable via `EXIT_PREMIUM_TARGET_PCT` env var (default 0.015 = 1.5%)
- `ExitReason.TARGET_HIT` returned
- Plugged into composite stack above `TimestopPolicy`

**DoD extras:**
- Backtest on today's trades: Trade 1 would have exited at +3% (target 3%) instead of +0.52%
- Unit test: `check()` returns `TARGET_HIT` when pnl_pct=0.02, target=0.015

---

### E2-S3: TrailingStopPolicy
**Points:** 3

**Story:** As a trader, I want a trailing stop that locks in profits once the trade moves in my favour, preventing the reversal pattern seen in trades 2, 6, 7.

**Acceptance Criteria:**
- `TrailingStopPolicy(activation_mfe=0.01, trail_pct=0.005)`
- Activates when `position.mfe_pct >= activation_mfe`
- Exits when `position.pnl_pct < position.mfe_pct - trail_pct`
- Never tightens (trail moves with MFE, never moves against)
- `ExitReason.TRAILING_STOP` returned
- `EXIT_TRAILING_ACTIVATION_PCT` and `EXIT_TRAILING_TRAIL_PCT` env vars

**DoD extras:**
- Unit test: MFE=1.5%, pnl=0.9% with trail=0.5% → TRAILING_STOP (0.9 < 1.5 - 0.5)
- Unit test: MFE=0.8% (below activation 1%) → None (not activated)
- Backtest: Trade 2 (MFE=1.14%, ended -0.64%) → trailing stop fires at ~0.64%, saves ~1.3%

---

### E2-S4: ThesisFailPolicy
**Points:** 2

**Story:** As a trader, I want early exit when a trade shows zero positive movement after N bars, catching "wrong direction from bar 1" trades before they drain.

**Acceptance Criteria:**
- `ThesisFailPolicy(min_bars=3, min_mfe_pct=0.002)` — exit if `bars_held >= 3` and `mfe_pct < 0.2%`
- `ExitReason.THESIS_FAIL` returned
- `EXIT_THESIS_FAIL_BARS` and `EXIT_THESIS_FAIL_MIN_MFE` env vars

**DoD extras:**
- Backtest: CE trades 3,4,5 (MFE=0%) all exit at bar 3 → saves ~0.3–0.5% each vs 2b timestop
- Unit test: bars_held=3, mfe=0 → THESIS_FAIL; bars_held=2 → None; mfe=0.003 → None

---

### E2-S5: Default exit stack wired to profiles
**Points:** 2

**Story:** As an operator, I want the standard exit stack active for all paper and live runs without code changes.

**Acceptance Criteria:**
- Default stack: `[PremiumTargetPolicy(0.015), TrailingStopPolicy(0.01, 0.005), ThesisFailPolicy(3), TimestopPolicy(15)]`
- Stack configurable per strategy profile in `runtime_config.json`
- Exit reason logged in decision trace: `exit_policy_triggered: "trailing_stop"` field
- Dashboard trade inspector shows `EXIT REASON: TRAILING_STOP` (not just TIME_STOP)

---

### E2-S6: Exit quality metrics in dashboard
**Points:** 2

**Story:** As a trader, I want to see daily exit quality metrics so I can tune the exit stack over time.

**Acceptance Criteria:**
- New `/api/strategy/observability/summary` fields: `avg_mfe_pct`, `avg_capture_ratio` (pnl/mfe)
- Dashboard shows: "MFE 1.71% · captured 30%" for the session
- Trade inspector shows `MFE`, `MAE`, `capture_ratio` for each trade (already in position docs, just surface it)

---

## E3 — Entry Gate Hardening
*Estimated: 2–3 days.*

---

### E3-S1: Align bypass min_confidence to entry gate
**Points:** 2

**Story:** As a trader, I want the consensus bypass to respect the entry gate threshold so sub-threshold entries are blocked.

**Acceptance Criteria:**
- `CONSENSUS_BYPASS_MIN_CONFIDENCE` env var (default `0.65`) used in `_process_entry_consensus`
- Replaces the current `self._min_confidence` (0.50) check in the consensus path only
- Main `_min_confidence` (0.50) unchanged — only consensus bypass threshold raised
- Backtest: today's trades — Trade 3 (CE 11:10, ep≈0.58) would have been blocked

**DoD extras:**
- Unit test confirms 0.58 < 0.65 → entry blocked in consensus path
- Existing tests pass (min_confidence for non-consensus path unchanged)

---

### E3-S2: SIDEWAYS minimum direction margin
**Points:** 2

**Story:** As a trader, I want a higher direction margin required in SIDEWAYS regime, since flat markets produce noisy signals.

**Acceptance Criteria:**
- `DIRECTION_MIN_MARGIN_SIDEWAYS` env var (default `2.0`, vs global default `1.25`)
- Applied in `resolve_direction_consensus` when `regime == SIDEWAYS`
- Trade 3 today (SIDEWAYS, margin=3.925 > 2.0) would still have passed — regression check
- A trade with margin=1.5 in SIDEWAYS would now be vetoed

---

## E4 — Risk Sizing
*Estimated: 3–4 days.*

---

### E4-S1: RiskCalculator interface + FixedFractionRisk
**Points:** 3

**Story:** As a trader, I want position size tied to capital at risk per trade so drawdowns are bounded regardless of premium level.

**Acceptance Criteria:**
- `RiskCalculator` ABC: `compute_lots(signal, risk_ctx, capital) -> int`
- `FixedFractionRisk(risk_pct=0.01)` — risks 1% of capital per trade
  - `lots = floor(capital * risk_pct / (entry_premium * lot_size * stop_loss_pct))`
- `RISK_CALCULATOR=fixed_fraction` env var; `RISK_FRACTION_PCT=0.01`
- Minimum 1 lot enforced
- Current `max_lots` calculation replaced (or wrapped) with this

**DoD extras:**
- Unit test: capital=500000, entry=1000, stop=0.4, risk_pct=0.01 → `floor(500000*0.01/(1000*15*0.004)) = 8 lots`
- Integration test: lots clipped by `max_lots` from risk manager

---

### E4-S2: Commission + slippage modelling in P&L
**Points:** 2

**Story:** As a trader, I want simulated P&L to deduct transaction costs so paper results are realistic.

**Acceptance Criteria:**
- `TRANSACTION_COST_PER_LOT=50` (INR, includes brokerage + STT + charges, approximate)
- Simulated P&L deducts `2 * transaction_cost * lots / (entry_premium * lot_size)` per trade
- Dashboard shows `net_pnl_pct` (after costs) and `gross_pnl_pct`
- Slippage (real vs sim) tracked once `execution_app` is live

---

## E5 — Observability & Alerts
*Estimated: 3–4 days.*

---

### E5-S1: Trade alert (Telegram / email)
**Points:** 3

**Story:** As a trader, I want immediate notification when a trade fires or closes so I can monitor the system without watching the dashboard.

**Acceptance Criteria:**
- Alert on `POSITION_OPEN`: "🟢 PE 54200 BUY @ 1122.60 · session trades: 1"
- Alert on `POSITION_CLOSE`: "🔴 PE 54200 CLOSED @ 1141.55 +1.69% · TIME_STOP"
- Alert on halt: "⚠️ HALTED consecutive_losses=3"
- Telegram bot preferred; email fallback
- `ALERT_TELEGRAM_TOKEN`, `ALERT_TELEGRAM_CHAT_ID` env vars
- `ALERT_ENABLED=0` disables (default off until tested)

---

### E5-S2: Kite token auto-refresh
**Points:** 3

**Story:** As an operator, I want the Kite access token refreshed automatically before market open so the system never fails due to expired credentials.

**Acceptance Criteria:**
- Cron job at 08:30 IST refreshes token via Kite login flow
- New token written to `KITE_ACCESS_TOKEN` env (or secrets file)
- `ingestion_app` and `execution_app` reload token without restart
- If refresh fails, alert sent and system falls back to paper mode
- Test: force token expiry → system detects and alerts within 60s

---

### E5-S3: Daily P&L report
**Points:** 2

**Story:** As a trader, I want an end-of-day summary with all trades, P&L, MFE/MAE, and gate-level analysis automatically generated.

**Acceptance Criteria:**
- Report generated at 15:40 IST (after market close)
- Contains: session P&L, trades table (entry/exit/pnl/mfe/mae/reason), blockers funnel, win rate
- Sent via Telegram and saved to `docs/reports/YYYY-MM-DD.md`
- Uses existing `/api/strategy/observability/summary` and `/api/strategy/blocker-funnel` endpoints

---

## E6 — Data Quality Fixes
*Estimated: 3 days.*

---

### E6-S1: run_id in MongoDB position docs
**Points:** 2

**Story:** As an engineer, I want `run_id` populated in `strategy_positions` MongoDB docs so multi-run days don't mix trades.

**Investigation first:** Why does `strategy_persistence_app` write `run_id: null`?
- Check: does `strategy_app` publish `run_id` in the position event?
- Check: does `strategy_persistence_app` read and forward it?

**Acceptance Criteria:**
- `strategy_positions` docs have `run_id` matching `runtime_config.json`
- `_latest_run_id_for_date` correctly scopes today's trades when multiple runs exist
- Test: restart strategy_app mid-day → new `run_id` → trades correctly scoped per run

---

### E6-S2: Dynamic ATM depth instruments
**Points:** 3

**Story:** As an operator, I want `DEPTH_FEED_INSTRUMENTS` automatically set to current ATM strikes so depth data is always relevant.

**Acceptance Criteria:**
- `depth_collector.py` queries `phase1_market_snapshots` at startup to find today's ATM strike
- Computes 6 strikes: ATM ± 300 pts (CE and PE for each)
- Updates `DEPTH_FEED_INSTRUMENTS` in-process and restarts polling with new instruments
- Re-runs ATM check every 30 minutes to follow large market moves
- Falls back to static `DEPTH_FEED_INSTRUMENTS` env var if snapshot query fails

---

### E6-S3: Direction ML model single load
**Points:** 1

**Story:** As an engineer, I want the direction ML model loaded once at startup, not every snapshot.

**Root cause:** `direction_ml_policy.py` is being re-initialized on every snapshot.

**Acceptance Criteria:**
- Model loads once at startup log: `direction_ml_policy: loaded model ...` appears exactly once
- Snapshot logs don't show model reload lines
- Performance: no measurable latency change per snapshot

---

### E6-S4: Brain day_score population
**Points:** 3

**Story:** As a trader, I want the Brain day_score computed correctly (not always UNKNOWN) so position sizing adapts to market conditions.

**Investigation first:** Why is `day_score: UNKNOWN` and `confidence: 0.0`?
- Check `brain_state.json` in `.run/strategy_app/`
- Check `brain.on_session_start()` — is daily context loading from `BRAIN_DAILY_FEATURES_PATH`?
- Check if the features file exists and has today's date

**Acceptance Criteria:**
- `brain_state.json` shows a real `day_score` (CALM/NEUTRAL/VOLATILE/AVOID) with `confidence > 0`
- Dashboard Brain widget shows non-UNKNOWN score
- `size_multiplier` reflects brain's assessment (< 1.0 on VOLATILE/AVOID days)

---

## E7 — Backtest Validation
*Estimated: 4–5 days. Run after E2 and E3 are complete.*

---

### E7-S1: Replay exit strategy variants
**Points:** 3

**Story:** As a trader, I want to compare exit strategy variants on historical data before committing to live, so I know the default stack is the best available option.

**Acceptance Criteria:**
- 3 variants replayed on last 20 trading days:
  - Variant A: current (TIME_STOP only)
  - Variant B: target=1.5% + trailing (E2 stack)
  - Variant C: target=2.0% + trailing
- Metrics per variant: P&L, win rate, avg MFE capture, max drawdown
- Results table in `docs/reports/exit_strategy_backtest_YYYY-MM-DD.md`
- Recommendation with reasoning

---

### E7-S2: Contra-regime veto backtest
**Points:** 2

**Story:** As a trader, I want to confirm the contra-regime veto improves P&L across historical data, not just today.

**Acceptance Criteria:**
- Replay last 20 trading days with and without contra-regime veto
- Metrics: trades blocked by veto, P&L saved, false positives (correct CE trades in bull regime blocked)
- Results in `docs/reports/contra_regime_backtest_YYYY-MM-DD.md`
- Net P&L improvement > 0 → veto kept. Net P&L negative → tune or remove.

---

### E7-S3: Entry gate threshold sensitivity
**Points:** 2

**Story:** As a trader, I want to know the optimal `CONSENSUS_BYPASS_MIN_CONFIDENCE` threshold from historical data.

**Acceptance Criteria:**
- Replay with thresholds: 0.50 (current), 0.60, 0.65, 0.70, 0.75
- Metrics per threshold: trades filtered, win rate, net P&L
- Optimal threshold identified and set as default in `CONSENSUS_BYPASS_MIN_CONFIDENCE`

---

## Sprint Plan

| Sprint | Epics | Duration | Goal |
|---|---|---|---|
| Sprint 1 | E1-S1, E1-S2, E2-S1, E2-S2, E2-S3, E3-S1 | 2 weeks | Execution bridge + exit quality foundation |
| Sprint 2 | E1-S3, E1-S4, E2-S4, E2-S5, E3-S2, E4-S1 | 2 weeks | `execution_app` live + full exit stack + sizing |
| Sprint 3 | E1-S5, E5-S1, E5-S2, E6-S1, E6-S3, E6-S4 | 2 weeks | Shadow mode + alerts + data quality |
| Sprint 4 | E1-S6, E2-S6, E4-S2, E5-S3, E7-S1, E7-S2, E7-S3 | 2 weeks | Production cutover + backtest validation |

**Target: Production live trading in 8 weeks.**

---

## Team Roles

| Role | Owns |
|---|---|
| Backend engineer (1) | E1 execution bridge, E6 data quality |
| Strategy engineer (1) | E2 exit policies, E3 entry gates, E7 backtests |
| DevOps / infra (0.5) | E5 alerts + monitoring, deployment |
| Trader | E7 backtest review, E1-S5 shadow sign-off, E1-S6 production cutover |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Kite order rejection (e.g. margin insufficient) | Medium | High | Pre-flight margin check before `place_order`; PaperAdapter fallback |
| Access token expiry during market hours | Medium | High | E5-S2 auto-refresh; alert if refresh fails |
| Slippage larger than expected on BANKNIFTY options | Medium | Medium | Shadow mode (E1-S5) measures this before going full live |
| Exit stack introduces new bugs in position management | Medium | High | Unit tests (E2-S1 DoD); replay with DoD verified against known trades |
| Brain day_score not fixable (missing features) | Low | Low | `size_multiplier` defaults to 1.0 if brain disabled — no loss of trading |
| Contra-regime veto blocks valid CE trades in a bull breakout | Low | Medium | E7-S2 backtest will surface this; tune or add `BREAKOUT_BULL` confidence threshold |
