# GO-LIVE CHECKLIST — verify before starting REAL money

*Run top to bottom on the runtime VM each morning before enabling real orders.
VM: `option-trading-runtime-01` (asia-south1-b). Dashboard: `http://34.14.171.45:8008/`.
Every step has a command, the GOOD result, and the action if it fails.
SSH prefix: `gcloud compute ssh option-trading-runtime-01 --zone asia-south1-b --command "..."`*

> **Golden rule:** if ANY §0 GO/NO-GO item fails, do NOT enable real money.
> Config source of truth = `ops/strategy_config.yml` (parity-checked).

---

## §0  GO / NO-GO pre-flight (hard blockers — all must be ✅)

| # | Check | Command | GOOD | If FAIL |
|---|---|---|---|---|
| 0.1 | **execution_app is UP** | `sudo docker ps --filter name=execution --format '{{.Names}} {{.Status}}'` | `...execution_app-1 Up (healthy)` | **BLOCKER (currently Exited 2d).** `cd /opt/option_trading && sudo docker compose --env-file .env.compose up -d execution_app`; recheck. No order reaches Dhan without it. |
| 0.2 | **Dhan IP whitelisted** | (Dhan dashboard) + after market: `db.execution_fills` recent `status` | no `Invalid IP` rejects | **BLOCKER.** Whitelist the VM's static IP `34.14.171.45` in the Dhan API portal. June had 478 `Invalid IP` rejects. |
| 0.3 | **Dhan token valid** | `grep DHAN_ACCESS_TOKEN /opt/option_trading/.env.compose` + a balance probe | token set, balance returns | Refresh Dhan token in `.env.compose`, recreate execution_app. |
| 0.4 | **Kite token valid** | ingestion_app healthy + fresh snapshot (see §2.1) | snapshots advancing | Refresh Kite token (`ingestion_app.token_refresh`); restart ingestion. |
| 0.5 | **Futures contract current** | snapshot `instrument` / futures symbol | `BANKNIFTY<curr-exp>FUT` (rolled) | Roll the futures symbol; stale contract = wrong prices. |
| 0.6 | **Real-money intent set** | `grep -E '^rollout_stage\|^EXECUTION_ADAPTER' .env.compose` | `EXECUTION_ADAPTER=dhan`; decide `rollout_stage` | If still validating, keep paper. Real orders gate on grade≥tier, not stage. |
| 0.7 | **1-lot cap** | `grep RISK_MAX_LOTS_PER_TRADE .env.compose` | `=1` | Set to 1. Never start real money above 1 lot. |

---

## §1  Config visual checks

| # | Check | Command | GOOD |
|---|---|---|---|
| 1.1 | **YAML ↔ .env.compose parity** | `cd /opt/option_trading && python3 ops/config_parity.py .env.compose --strict; echo $?` | `No real differences` + exit `0` |
| 1.2 | **Live config (UI panel)** | open `http://34.14.171.45:8008/` → OPS panel, OR `curl -s localhost:8008/api/ops/config` | 60 keys, values below |
| 1.3 | **Critical values** | (from 1.2) | `ENTRY_VOL_GATE_ENABLED=0`, `ENTRY_ML_MODEL_PATH=/app/ml_pipeline_2/artifacts/entry_only/published/velocity_base_entry_bundle.joblib`, `ENTRY_ML_MIN_PROB=0.049` (top-10% quantile), `ML_ENTRY_DIRECTION_MODE=regime_dual`, `STRATEGY_STRIKE_SELECTION_POLICY=otm`, `SMART_STRIKE_MAX_PREMIUM=1300`, `EXIT_STRATEGY_MODE=adaptive`, `EXIT_MAX_LOSS_PCT=0.1`, `RISK_MAX_SESSION_TRADES=6`, `RISK_MAX_CONSECUTIVE_LOSSES=6`, `STRATEGY_PROFILE_ID=trader_master_live_v1` |
| 1.4 | **Startup applied config** | `sudo docker logs option_trading-strategy_app-1 2>&1 \| grep 'strategy_config applied' \| tail -1` | `60/60 keys ... real_overrides=0` |

---

## §2  Upstream data — is everything arriving cleanly?

| # | Check | Command | GOOD |
|---|---|---|---|
| 2.1 | **Snapshots fresh** (run during market hrs) | mongo: latest `phase1_market_snapshots.market_time_ist` | within ~1–2 min of now (IST) |
| 2.2 | **Snapshot fields complete** | inspect latest snapshot: `futures_derived.atr_ratio`, `.price_vs_vwap`, `chain_aggregates.atm_straddle_price`, `atm_options.atm_ce_close/atm_pe_close`, `opening_range` | all non-null (not NaN). NaNs → degraded entry/direction. |
| 2.3 | **Ingestion healthy** | `sudo docker ps --filter name=ingestion` | `Up (healthy)` |
| 2.4 | **Redis snapshot stream flowing** | `sudo docker exec option_trading-redis-1 redis-cli --scan --pattern '*snapshot*' \| head` + check it updates | keys present + advancing |
| 2.5 | **VIX/India VIX present** (gates use it) | redis `live:websocket:tick:INDIAVIX:latest` | recent tick |

```bash
# 2.1/2.2 one-liner
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval '
var d=db.phase1_market_snapshots.find().sort({market_time_ist:-1}).limit(1).next();
var s=d.payload.snapshot; print("latest="+d.market_time_ist);
print("atr="+s.futures_derived.atr_ratio+" vwap="+s.futures_derived.price_vs_vwap+
" straddle="+s.chain_aggregates.atm_straddle_price+" ce="+s.atm_options.atm_ce_close);'
```

---

## §3  Each gate / member sequentially verified (from strategy_app logs)

Run during market hours; tail the live engine and confirm each stage fires.
`L(){ sudo docker logs --since 10m option_trading-strategy_app-1 2>&1 | grep -i "$1" | tail -3; }`

| # | Gate / member | grep | GOOD |
|---|---|---|---|
| 3.1 | Engine consuming bars | `L "consumed events"` or `L "snapshot"` | count advancing each minute |
| 3.2 | **Regime** classify | `L "regime"` | a Regime enum per bar (TRENDING/SIDEWAYS/…); CHOP/AVOID → no entry |
| 3.3 | **Time gate** | `L "entry_time_windows"` | only blocks outside 09:45–14:30 |
| 3.4 | **Entry trigger (vol gate OR ML)** | If `ENTRY_VOL_GATE_ENABLED=1`: `L "vol_gate"` → `atr_pct=…>=0.00088`. If `=0` (ML_ENTRY): `L "ml_entry"` → `entry_prob=…>=0.049` on velocity_base bundle | fires on qualifying bars |
| 3.5 | **MLEntry / bypass** | `L "ml_confidence_below_bypass"` or entry_gate trace | If ML_ENTRY: passes ≥0.049; if VOL_GATE: passes ≥0.65 |
| 3.6 | **Direction** | `L "direction_source"` | `regime_dual…` (or `conviction_ensemble`), CE/PE resolved |
| 3.7 | **Strike** | `L "strike"` / `L "premium"` | OTM strike in ₹600–1300, not vetoed |
| 3.8 | **Exit stack built** | `sudo docker logs option_trading-strategy_app-1 2>&1 \| grep -i "exit policy\|exit_stack" \| tail -1` | `composite[hard_stop_10%, adaptive[lottery=BREAKOUT,TRENDING\|scalper=rest]]` |
| 3.9 | **No tracebacks** | `L "error\|traceback\|exception"` | none |

> Tip: the OPS-panel SIM on today's date reproduces this gate-by-gate trace
> (`diag` + `analysis.markdown`) without waiting for live bars.

---

## §4  Redis / DB — data flowing & persisted

| # | Check | Command | GOOD |
|---|---|---|---|
| 4.1 | redis healthy | `sudo docker exec option_trading-redis-1 redis-cli ping` | `PONG` |
| 4.2 | mongo healthy | `sudo docker ps --filter name=mongo` | `Up (healthy)` |
| 4.3 | snapshots writing | `db.phase1_market_snapshots.countDocuments({trade_date_ist:"<today>"})` | grows through the day |
| 4.4 | signals writing | `db.trade_signals.countDocuments({trade_date_ist:"<today>"})` | grows when entries fire |
| 4.5 | positions writing | `db.strategy_positions.countDocuments({trade_date_ist:"<today>", run_id:null})` | live (null run_id) positions on entries |
| 4.6 | persistence apps up | `sudo docker ps --filter name=persistence` | all `Up (healthy)` |
| 4.7 | **no replay contamination** | live positions = run_id `null`/`paper-*`; sims = `sim-*`/`ops-sim-*` | don't confuse — see `ops/live_ledger.js` |

---

## §5  Dhan / execution path (the real-money leg)

| # | Check | Command | GOOD |
|---|---|---|---|
| 5.1 | execution_app UP | (= §0.1) | `Up (healthy)` |
| 5.2 | adapter = dhan | `sudo docker exec option_trading-execution_app-1 printenv \| grep EXECUTION_ADAPTER` | `dhan` |
| 5.3 | consuming signals | `sudo docker logs --since 10m option_trading-execution_app-1 2>&1 \| grep "execution consumer: signal" \| tail` | logs each signal |
| 5.4 | **tier gate** | `printenv \| grep EXECUTION_REQUIRE_LIVE_TIER` | `1` (only tier==live executes) |
| 5.5 | **sim-block active** | (code) any `run_id sim-*` → `BLOCKED sim signal` in logs | sims never reach broker |
| 5.6 | fills land | `db.execution_fills` newest doc `status` | `filled` with real `order_id` (NOT `paper_*`, NOT `Invalid IP`) |
| 5.7 | balance / margin | Dhan probe | enough for 1 lot |

> First real signal of the day: WATCH §5.3 + §5.6 live. If `Invalid IP` → halt
> (§0.2 not done). If `paper_*` order_id → it's still paper, not real.

---

## §6  Risk & kill-switch

| # | Check | GOOD |
|---|---|---|
| 6.1 | 1 lot cap | `RISK_MAX_LOTS_PER_TRADE=1` |
| 6.2 | session trade cap | `RISK_MAX_SESSION_TRADES=6` |
| 6.3 | consec-loss halt | `RISK_MAX_CONSECUTIVE_LOSSES=6` |
| 6.4 | universal loss floor | `EXIT_MAX_LOSS_PCT=0.10` |
| 6.5 | **KILL SWITCH** | to stop real orders instantly: `sudo docker stop option_trading-execution_app-1` (strategy keeps running paper; no orders reach Dhan). To halt strategy too: stop `strategy_app`. |

---

## §7  During-day monitoring

- OPS panel (`/`) for live positions + P&L.
- `sudo docker logs -f option_trading-strategy_app-1` — entries/exits/regime.
- `sudo docker logs -f option_trading-execution_app-1` — fills/rejects.
- After close: clean P&L via `ops/live_ledger.js` (strips replay noise; live = run_id null/paper-*).

---

## §8  Current known state (as of 2026-06-17)

- ❌ **execution_app EXITED 2 days** → §0.1 must be fixed.
- ❌ **Dhan `Invalid IP`** (478 June rejects) → §0.2 whitelist `34.14.171.45`.
- ⚠️ **No real fills ever** — June "trades" were paper (`paper_*`) / replay-contaminated. First real fill is unproven; watch §5.6 closely.
- ⚠️ **Depth not collected** (`DEPTH_FEED_INSTRUMENTS` empty) — not required for go-live, but a known gap.
- ⚠️ **Entry model switched 2026-06-17:** `VOL_GATE_ENTRY` → `ML_ENTRY` with `velocity_base` bundle at `ENTRY_ML_MIN_PROB=0.049` (top-10% quantile). Bundle is research-grade (`ml_pipeline_2_research_model_package` re-packaged as `entry_only_bundle`).
  - **Morning-session risk:** before 11:30 IST, 39/54 velocity features are NaN (imputed to medians). Model may produce unreliable probabilities in first 90 minutes.
  - **vol_spike_ratio permanently NaN** — no 20-day rolling context available.
  - **Awaiting first live-bar score** — bundle loads on first snapshot tomorrow (2026-06-18) at 09:45 IST.
- ✅ Config single-source + parity, SIM≡LIVE engine, sim→Dhan leak closed, 1-lot cap, exit stacks correct.

**Bottom line:** the *strategy/config/data* path is ready; the *execution* path (§0.1, §0.2) is NOT. Real money cannot start until execution_app is up AND the Dhan IP is whitelisted AND a first real `filled` order (not `paper_*`, not `Invalid IP`) is confirmed. The ML_ENTRY switch adds an additional validation requirement: confirm the velocity_base bundle loads cleanly and scores sensibly on the first live bars.
