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

**2026-09-25: the seller now has the same file-based `operator_halt` the
buyer has** (`strategy_app/seller/runner.py`, `SellerRunner.on_snapshot()`,
resolved via the same `resolve_runtime_artifact_paths()` helper the buyer
uses). Same semantics as the buyer: blocks new entries only, on the next
30s poll, no restart needed, persists across restarts. Existing open
spreads are still actively managed and closed — including the crash-veto
tripwire and every normal exit condition (take-profit, stop, DTE, max-hold)
— exactly like the buyer's halt never blocks exits. **This closes the "no
live-settable halt for the seller" gap this doc used to document below.**

One real difference from the buyer: the seller's `STRATEGY_RUN_DIR`
(`/seller_run`) is a **Docker-managed named volume, not a host bind-mount**
— there is no `/opt/option_trading/.run/...` path to `touch` directly from
the host shell. Go through the container (or, for a stopped one, a
throwaway container attached to the same named volume):

```bash
# Halt a RUNNING seller (block new entries; existing spreads still managed):
sudo docker exec option_trading-seller_app_sensex-1 touch /seller_run/operator_halt
sudo docker exec option_trading-seller_app_finnifty-1 touch /seller_run/operator_halt

# Resume a running seller:
sudo docker exec option_trading-seller_app_sensex-1 rm -f /seller_run/operator_halt

# Halt/resume a STOPPED seller's volume directly (BankNifty, NIFTY — both
# currently SIGKILLed since 2026-09-08; this pre-arms the halt so if either
# is ever resumed it comes up already halted, requiring an explicit second
# step to actually allow entries):
sudo docker run --rm -v option_trading_seller_run:/seller_run alpine touch /seller_run/operator_halt        # BankNifty
sudo docker run --rm -v option_trading_seller_nifty_run:/seller_run alpine touch /seller_run/operator_halt  # NIFTY

# Check current halt status:
sudo docker exec option_trading-seller_app_sensex-1 test -f /seller_run/operator_halt && echo HALTED || echo "not halted"
```

**Stopping the container entirely is still the right tool for a real
emergency** (the halt file only blocks NEW entries — it does not stop the
process, and a bug in the manage/close path itself, like the 10-day stuck
close this repo found on 2026-09-25, keeps running under either mechanism).
Confirm zero open positions before a full container stop, same as before:

```bash
sudo docker exec option_trading-mongo-1 mongosh --quiet trading_ai --eval '
db.seller_positions.countDocuments({instrument:"BANKNIFTY", status:{$in:["OPEN","open"]}})
'
sudo docker stop option_trading-seller_app-1 option_trading-seller_app_nifty-1   # BankNifty + NIFTY

# Resume: recreate it (picks up current .env.compose)
cd /opt/option_trading && sudo docker compose --env-file .env.compose -f docker-compose.gcp.yml up -d --no-deps seller_app seller_app_nifty
```

If there are open positions, do not `docker stop` the seller — either wait
for it to close them on its own risk logic, or manage the close manually
before stopping.
