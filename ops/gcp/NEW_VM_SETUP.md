# New VM Setup — Complete Runbook

Branch to deploy: `feat/entry-pipeline-refactor-gate-cascade`

---

## 1. Clone repo + checkout branch

```bash
cd /opt
sudo git clone https://github.com/amitsajwan/option_trading.git option_trading
cd /opt/option_trading
sudo git checkout feat/entry-pipeline-refactor-gate-cascade
sudo git log -1 --oneline   # verify HEAD
```

---

## 2. Copy secrets + credentials

From the old VM or secure store:

```bash
# Kite credentials
sudo cp /path/to/credentials.json /opt/option_trading/secrets/credentials.json

# TOTP secret (for daily token refresh)
sudo cp /path/to/.env.totp /opt/option_trading/.env.totp
```

---

## 3. Configure .env.compose

Start from the example and apply all required overrides:

```bash
sudo cp /opt/option_trading/.env.compose.example /opt/option_trading/.env.compose
```

### Required overrides (minimum working set)

```bash
# ── Instrument ────────────────────────────────────────────────────────────────
INSTRUMENT_SYMBOL=BANKNIFTY26JUNFUT        # update monthly at expiry
NIFTY_FUT_SYMBOL=NFO:NIFTY26JUNFUT

# ── Entry window (matches model training distribution) ────────────────────────
ENTRY_WINDOW_START_IST=09:45
ENTRY_WINDOW_END_IST=14:30

# ── Strategy ──────────────────────────────────────────────────────────────────
STRATEGY_MIN_CONFIDENCE=0.80
CONSENSUS_BYPASS_MIN_CONFIDENCE=0.80
STRATEGY_PROFILE_ID=trader_master_live_v1

# ── Strike selection: ₹600-1300 ATM band ─────────────────────────────────────
STRATEGY_STRIKE_SELECTION_POLICY=smart_strike
STRATEGY_SMART_STRIKE_ENABLED=1
SMART_STRIKE_MIN_PREMIUM=600
SMART_STRIKE_MAX_PREMIUM=1300
SMART_STRIKE_HARD_PREMIUM_CAP=1
STRATEGY_STRIKE_MAX_OTM_STEPS=4

# OTM tiers — regime-gated (CHOP blocked in code, OTM3/4 need BREAKOUT/TRENDING)
SMART_STRIKE_OTM2_ENABLED=1
SMART_STRIKE_OTM3_ENABLED=1
SMART_STRIKE_OTM3_REGIMES=BREAKOUT,TRENDING
SMART_STRIKE_OTM3_MAX_BAR_HOUR=12
SMART_STRIKE_OTM4_ENABLED=1
SMART_STRIKE_OTM4_REGIMES=BREAKOUT
SMART_STRIKE_OTM4_MAX_BAR_HOUR=11
SMART_STRIKE_OTM4_MIN_OI=50000
SMART_STRIKE_OTM_IV_CEIL=92

# ── Direction consensus ────────────────────────────────────────────────────────
# Direction ML AUC=0.557 (near random) — keep weight low until retrained
DIRECTION_CONSENSUS_ML_WEIGHT=0.15
DIRECTION_CONSENSUS_RULE_WEIGHT=1.0
DIRECTION_CONSENSUS_SHADOW_WEIGHT=1.0
DIRECTION_CONSENSUS_MOMENTUM_WEIGHT=0.75
DIRECTION_CONSENSUS_MIN_MARGIN=1.75
DIRECTION_MIN_MARGIN_SIDEWAYS=2.0

# Direction-evidence gate (new): block trade if regime evidence opposes direction
DIRECTION_EVIDENCE_SUPPORT_MIN=0.2
DIRECTION_EVIDENCE_OPPOSING_MAX=0.6

# ── Exit — adaptive ────────────────────────────────────────────────────────────
EXIT_STRATEGY_MODE=adaptive
EXIT_POLICY_STACK_ENABLED=1
EXIT_PREMIUM_TARGET_PCT=0.03
EXIT_TRAILING_ACTIVATION_PCT=0.015
EXIT_TRAILING_TRAIL_PCT=0.008
EXIT_THESIS_FAIL_BARS=5
EXIT_THESIS_FAIL_MIN_MFE=0.003

# Lottery exit (BREAKOUT/TRENDING only — ATM-tuned)
LOTTERY_HARD_STOP_PCT=0.15
LOTTERY_BIG_TARGET_PCT=0.40
LOTTERY_RUNNER_ACTIVATION_MFE=0.15
LOTTERY_RUNNER_GIVEBACK_FRAC=0.30
LOTTERY_THESIS_FAIL_BARS=5
LOTTERY_THESIS_FAIL_MIN_MFE=0.03
LOTTERY_TIMESTOP_BARS=60
LOTTERY_MOMENTUM_FLIP=1.0

# ── Risk ──────────────────────────────────────────────────────────────────────
RISK_MAX_SESSION_TRADES=20
RISK_MAX_CONSECUTIVE_LOSSES=15
RISK_MAX_DAILY_LOSS_PCT=0.03
RISK_MAX_LOTS_PER_TRADE=5

# Cooldowns (discipline gates)
MIN_REENTRY_BARS=3
STOP_LOSS_COOLDOWN_BARS=5
DIRECTION_FLIP_COOLDOWN_BARS=8
ZERO_MFE_COOLDOWN_BARS=10

# ── Paper trading ─────────────────────────────────────────────────────────────
ROLLOUT_STAGE=paper
STRATEGY_CAPITAL_ALLOCATED=500000
```

---

## 4. Build and start

```bash
cd /opt/option_trading

# Build all images
sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  build strategy_app dashboard

# Start full stack
sudo docker compose --env-file .env.compose \
  -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d
```

---

## 5. Install daily token refresh

```bash
sudo bash /opt/option_trading/ops/gcp/install_token_refresh_timer.sh
sudo systemctl status kite-token-refresh.timer   # should show active (waiting)
```

The timer fires at **03:00 UTC = 08:30 IST** — before market open.

---

## 6. Verify at startup

```bash
# All containers healthy?
sudo docker ps --format '{{.Names}}\t{{.Status}}' | sort

# Correct config loaded?
sudo docker logs option_trading-strategy_app-1 2>&1 | grep 'entry_config_effective'
# Expected: max_premium=1300 hard_cap=True strike_policy=smart_strike

# Exit mode correct?
sudo docker logs option_trading-strategy_app-1 2>&1 | grep 'exit policy mode'
# Expected: adaptive[lottery=BREAKOUT,TRENDING|scalper=rest]

# CHOP blocked in routing?
sudo docker logs option_trading-strategy_app-1 2>&1 | grep 'CHOP'
# Expected: CHOP -> [] (empty strategies = no entries in CHOP)
```

---

## 7. Verify live (after 09:45)

One grep to see everything:

```bash
sudo docker logs option_trading-strategy_app-1 2>&1 | \
  grep -E 'entry blocked|entry signal|position opened|position closed' | \
  grep -v 'XGBoost'
```

Each blocked bar now shows exactly why:
```
entry blocked: direction_evidence_mismatch dir=PE bull_score=0.80 > 0.60 bear_score=0.00 < 0.20
entry blocked: sideways_returns_mixed reason=...
entry blocked: zero_mfe_cooldown last_dir=PE bars_since=4 cool=10
entry signal regime=TRENDING strategy=ML_ENTRY dir=CE strike=53800 premium=1012 conf=0.85
```

---

## 8. Future-deploy cycle (branch already pulled)

```bash
cd /opt/option_trading
git pull origin feat/entry-pipeline-refactor-gate-cascade
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
  build strategy_app dashboard
sudo docker compose --env-file .env.compose -f docker-compose.yml -f docker-compose.gcp.yml \
  up -d --no-deps --force-recreate --pull never strategy_app dashboard
```

**Never restart strategy_app during market hours (09:15–15:30 IST)** unless fixing a critical issue — intra-session restart loses the in-memory tracker state and orphans open positions in positions.jsonl.
