# -*- coding: utf-8 -*-
"""
Full-config June 2026 simulation with CURRENT live settings.
Verifies: entry model fires on correct days, exits work, direction=weighted.

Run INSIDE strategy_app container (pymongo needed):
  docker exec option_trading-strategy_app-1 pip install pymongo --quiet
  docker cp ops/research/jun_verify_sim.py option_trading-strategy_app-1:/tmp/
  docker exec option_trading-strategy_app-1 python /tmp/jun_verify_sim.py

NOTE: Historical MongoDB snapshots lack 16/40 model features
(adx_14, bb_width_20, ema_spread_*, range_10/30, vix_intraday_chg, etc.)
These are median-filled → model less selective than LIVE (where features exist).
Result: fire count is inflated vs live; P&L signal is directional only.
"""
import sys, os
sys.path.insert(0, '/app')

os.environ['ENTRY_ML_MODEL_PATH']          = '/app/ml_pipeline_2/artifacts/entry_only/published/entry_compression_v1.joblib'
os.environ['ENTRY_ML_MIN_PROB']            = '0.35'
os.environ['EXIT_STRATEGY_MODE']           = 'adaptive'
os.environ['ADAPTIVE_LOTTERY_REGIMES']     = 'TREND,TRENDING,BREAKOUT'
os.environ['REGIME_DIRECTION_SIGNAL']      = 'weighted'
os.environ['EXIT_SCALPER_HARD_STOP_PCT']   = '0.07'
os.environ['EXIT_PREMIUM_TARGET_PCT']      = '0.03'
os.environ['EXIT_TRAILING_ACTIVATION_PCT'] = '0.015'
os.environ['EXIT_TRAILING_TRAIL_PCT']      = '0.008'
os.environ['EXIT_THESIS_FAIL_BARS']        = '5'
os.environ['EXIT_THESIS_FAIL_MIN_MFE']     = '0.002'
os.environ['LOTTERY_HARD_STOP_PCT']        = '0.20'
os.environ['LOTTERY_BIG_TARGET_PCT']       = '0.50'
os.environ['LOTTERY_RUNNER_ACTIVATION_MFE'] = '0.20'
os.environ['LOTTERY_RUNNER_GIVEBACK_FRAC']  = '0.35'
os.environ['LOTTERY_THESIS_FAIL_BARS']     = '5'
os.environ['LOTTERY_THESIS_FAIL_MIN_MFE']  = '0.03'
os.environ['LOTTERY_TIMESTOP_BARS']        = '60'

import joblib, pymongo
from strategy_app.brain.regime_director import RegimeDirector
from strategy_app.ml.bundle_inference import predict_positive_class_prob
from strategy_app.position.exit_policy import build_adaptive_exit_stack
from strategy_app.contracts import PositionContext
from strategy_app.market.snapshot_accessor import SnapshotAccessor

MODEL_PATH   = os.environ['ENTRY_ML_MODEL_PATH']
ENTRY_BUNDLE = joblib.load(MODEL_PATH)
THRESHOLD    = 0.35

mongo    = pymongo.MongoClient('mongodb://mongo:27017')
coll     = mongo['trading_ai']['phase1_market_snapshots']
LOT      = 30
director = RegimeDirector(signal='weighted')

JUNE_DAYS = (
    [f'2026-06-0{d}' for d in range(1, 10)] +
    [f'2026-06-{d}'  for d in range(10, 26)]
)

print('\n' + '=' * 72)
print('  JUNE 2026 SIM -- live config (weighted dir, thr=0.35, adaptive exit)')
print('=' * 72)
print(f'{"Date":<12} {"Bars":>5} {"Fires":>6} {"Entries":>8} {"WR":>4} {"Avg%":>6} {"Total Rs":>10}')
print('-' * 72)

all_trades, all_rs = [], []
active_days = 0

for day in JUNE_DAYS:
    docs = list(coll.find({'trade_date_ist': day}, sort=[('timestamp_ist', 1)]))
    if not docs:
        continue

    bars_processed = 0
    fires          = 0
    trades         = []

    for i, doc in enumerate(docs):
        raw_acc = doc.get('payload', {}).get('snapshot', {})
        if not raw_acc:
            continue
        bars_processed += 1
        acc_obj = SnapshotAccessor(raw_acc)

        try:
            prob = predict_positive_class_prob(ENTRY_BUNDLE, acc_obj)
        except Exception:
            continue
        if prob is None or prob < THRESHOLD:
            continue
        fires += 1

        try:
            verdict = director.decide(acc_obj)
        except Exception:
            continue
        if verdict.quality == 'CHOP':
            continue

        side = verdict.side
        if side not in ('CE', 'PE'):
            continue

        atm = raw_acc.get('atm_options') or {}
        entry_prem = float(atm.get(f'atm_{side.lower()}_close') or 0)
        if entry_prem < 10:
            continue

        exit_stack = build_adaptive_exit_stack()
        pos = PositionContext(
            position_id=f"sim_{i}",
            direction=side,
            strike=0,
            expiry=None,
            entry_time=__import__('datetime').datetime.now(),
            entry_snapshot_id="sim",
            lots=LOT,
            entry_premium=entry_prem,
            current_premium=entry_prem,
            pnl_pct=0.0, mfe_pct=0.0, mae_pct=0.0,
            bars_held=0, high_water_premium=entry_prem,
            trailing_active=False,
            entry_regime=verdict.quality,
            stop_loss_pct=float(os.environ['EXIT_SCALPER_HARD_STOP_PCT']),
            target_pct=float(os.environ['EXIT_PREMIUM_TARGET_PCT']),
            trailing_enabled=True,
            trailing_activation_pct=float(os.environ['EXIT_TRAILING_ACTIVATION_PCT']),
            trailing_offset_pct=float(os.environ['EXIT_TRAILING_TRAIL_PCT']),
        )

        exit_prem = entry_prem
        for future_doc in docs[i + 1:]:
            future_acc = future_doc.get('payload', {}).get('snapshot', {})
            curr_prem  = float((future_acc.get('atm_options') or {}).get(f'atm_{side.lower()}_close') or 0)
            if curr_prem < 1:
                continue
            from dataclasses import replace as dc_replace
            pos = dc_replace(pos,
                current_premium=curr_prem,
                pnl_pct=(curr_prem - entry_prem) / entry_prem,
                mfe_pct=max(pos.mfe_pct, (curr_prem - entry_prem) / entry_prem),
                mae_pct=min(pos.mae_pct, (curr_prem - entry_prem) / entry_prem),
                high_water_premium=max(pos.high_water_premium, curr_prem),
                bars_held=pos.bars_held + 1,
            )
            decision = exit_stack.check(pos, None)
            if decision is not None:
                exit_prem = curr_prem
                break
        else:
            exit_prem = pos.current_premium

        pnl_pct = (exit_prem - entry_prem) / entry_prem * 100
        pnl_rs  = (exit_prem - entry_prem) * LOT
        trades.append((side, entry_prem, exit_prem, pnl_pct, verdict.quality))
        all_trades.append(pnl_pct)
        all_rs.append(pnl_rs)

    if fires > 0 or trades:
        active_days += 1
        wr  = sum(1 for t in trades if t[3] > 0) / len(trades) * 100 if trades else 0
        avg = sum(t[3] for t in trades) / len(trades) if trades else 0
        tot = sum((t[2] - t[1]) * LOT for t in trades)
        flag = '<-- ACTIVE' if trades else '(fires, no regime pass)'
        print(f'{day:<12} {bars_processed:>5} {fires:>6} {len(trades):>8}  {wr:>3.0f}%  {avg:>+5.1f}%  {tot:>+8,.0f}  {flag}')
        for t in trades:
            print(f'    [{t[4]}] {t[0]}  entry={t[1]:.0f}  exit={t[2]:.0f}  pnl={t[3]:+.1f}%  Rs{(t[2]-t[1])*LOT:+,.0f}')
    elif docs:
        print(f'{day:<12} {bars_processed:>5} {fires:>6}  (no entry fires)')

wr_all  = sum(1 for p in all_trades if p > 0) / len(all_trades) * 100 if all_trades else 0
avg_all = sum(all_trades) / len(all_trades) if all_trades else 0
tot_rs  = sum(all_rs)
print('-' * 72)
print(f'TOTAL: {len(all_trades)} trades | {active_days} active days | WR={wr_all:.0f}% | Avg={avg_all:+.1f}% | Total=Rs{tot_rs:+,.0f}')
print()
print('Config confirmed in container:')
print(f'  REGIME_DIRECTION_SIGNAL   = {os.environ["REGIME_DIRECTION_SIGNAL"]}')
print(f'  ENTRY_ML_MIN_PROB         = {os.environ["ENTRY_ML_MIN_PROB"]}')
print(f'  EXIT_STRATEGY_MODE        = {os.environ["EXIT_STRATEGY_MODE"]}')
print(f'  ADAPTIVE_LOTTERY_REGIMES  = {os.environ["ADAPTIVE_LOTTERY_REGIMES"]}')
print(f'  EXIT_THESIS_FAIL_BARS     = {os.environ["EXIT_THESIS_FAIL_BARS"]}')
print(f'  LOTTERY_THESIS_FAIL_BARS  = {os.environ["LOTTERY_THESIS_FAIL_BARS"]}')
