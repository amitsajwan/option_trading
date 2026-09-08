# Emergency stop / resume — halting live trading without touching code

This documents the actual mechanism used to halt real-money trading
mid-session (first used 2026-09-08, on user instruction after adverse
market conditions). The buyer and seller are two **separate processes with
separate risk-management code** — stopping one does not stop the other.
There is currently no single command that halts everything; both steps
below are required.

## Buyer (`strategy_app` + `execution_app`)

`strategy_app/risk/manager.py`'s `RiskManager.is_halted` checks, on every
bar, whether a file called `operator_halt` exists at the instrument's
runtime directory (`resolve_runtime_artifact_paths()`, which resolves from
the `STRATEGY_RUN_DIR` env var). When present, new live-tier entries are
blocked (`strategy_app/risk/manager.py` ~L274: `"halted:operator_halt"`) —
existing code paths keep running otherwise, so this does **not** stop
position management, exits, or paper/shadow evaluation, only new
live-tier entries.

**Effective immediately on the next bar** — no container restart needed,
since the check is a filesystem read, not a startup-time config value. It
also **persists across restarts** (it's a file, not in-memory state), so it
carries into the next trading session automatically until explicitly
removed.

Per-instrument paths (host side, from `/opt/option_trading`):

| Instrument | `STRATEGY_RUN_DIR` | Halt file |
|---|---|---|
| BankNifty (primary) | `/app/.run/strategy_app` | `.run/strategy_app/operator_halt` |
| NIFTY | `/app/.run/strategy_app_nifty` | `.run/strategy_app_nifty/operator_halt` |
| FINNIFTY | `/app/.run/strategy_app_finnifty` | `.run/strategy_app_finnifty/operator_halt` |
| SENSEX | `/app/.run/strategy_app_sensex` | `.run/strategy_app_sensex/operator_halt` |

```bash
# Halt an instrument's buyer (block new live entries):
sudo touch /opt/option_trading/.run/strategy_app/operator_halt          # BankNifty
sudo touch /opt/option_trading/.run/strategy_app_nifty/operator_halt    # NIFTY

# Resume (no restart needed, takes effect on the next bar):
sudo rm /opt/option_trading/.run/strategy_app/operator_halt
sudo rm /opt/option_trading/.run/strategy_app_nifty/operator_halt

# Check current halt status for an instrument:
ls /opt/option_trading/.run/strategy_app/operator_halt 2>/dev/null && echo HALTED || echo not halted
```

`RiskManager.is_halted` also returns true for other reasons
(`daily_loss_breached`, `session_trade_cap_breached`, `weekly_loss_breached`)
— those are automatic, not operator-controlled. `halt_reason` on the risk
state distinguishes `"operator_halt"` from the automatic triggers; check a
recent decision trace's `risk_state.halt_reason` field to see which applies.

## Seller (`strategy_app.seller`)

The seller runs as a **separate process** (`python -m strategy_app.seller`,
`strategy_app/seller/manager.py`) with its own risk manager that does
**not** implement the `operator_halt` file check — it was built
independently and this gap has not been closed. There is no live-settable
halt for the seller today. To stop it, stop the container:

```bash
sudo docker stop option_trading-seller_app-1 option_trading-seller_app_nifty-1   # BankNifty + NIFTY

# Resume: recreate it (picks up current .env.compose)
cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.gcp.yml up -d --no-deps seller_app seller_app_nifty
```

**Before stopping a seller container, confirm zero open positions first** —
unlike the buyer's file-based halt (which keeps exit/position-management
code running), stopping the container entirely removes ALL monitoring for
that instrument, including any open position's exit logic:

```bash
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval '
db.seller_positions.countDocuments({instrument:"BANKNIFTY", status:{$in:["OPEN","open"]}})
'
```

If there are open positions, do not `docker stop` the seller — either wait
for it to close them on its own risk logic, or manage the close manually
before stopping.

## Known gap

The seller lacks the buyer's file-based halt entirely. Adding an
equivalent `operator_halt` check to `strategy_app/seller/manager.py` would
let both sides use the same fast, restart-free, position-safe mechanism —
not yet done.
