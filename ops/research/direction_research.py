"""Direction research — which signals predict WHICH WAY the move goes? (board B-3.x)

The cost-aware e2e (brain_backtest.py) proved direction is the binding constraint:
the move/cost path is profitable at perfect direction but needs ~0.74 accuracy, and we
have ~0.59. This harness measures, ON OUR DATA, whether any structural/momentum signal
predicts the *sign* of the next 10-min move — especially on the bars the brain would
trade (loaded springs). It does NOT train a model; it scores transparent trader rules so
we can build a DirectionSense from whatever actually works (and abstain where nothing does).

Honesty: direction has a documented ~0.59 ceiling and our sample is small/quiet. A rule
that doesn't clear ~0.55 with real coverage here is NOT a signal — we report it plainly.

RUN on the VM:
    docker compose run --rm --no-deps -v .../ops:/app/ops --entrypoint sh strategy_app \
        -c 'pip install pymongo -q; python ops/research/direction_research.py'
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from strategy_app.senses.context import (
    BASE_WINDOW, BUILD_WINDOW, COMPRESS_RATIO, OI_BUILD, WARMUP, _atr, _structure_for_bar,
)

HORIZON = 10
MIN_MOVE_PT = 50.0      # only judge direction on bars that actually moved (flat bars have no "side")


def _sign(x: Optional[float]) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else -1 if x < 0 else 0


# ---- direction predictors: each returns +1 (CE/up) | -1 (PE/down) | 0 (abstain) ----

def _predictors() -> dict[str, Callable[[dict], int]]:
    return {
        "momentum_1m": lambda f: _sign(f["ret_1m_pt"]),
        "momentum_5m": lambda f: _sign(f["fut_return_5m"]),
        "breakout": lambda f: {"up": 1, "down": -1}.get(f["struct_breakout"], 0),
        "trend_struct": lambda f: {"up": 1, "down": -1}.get(f["struct_trend"], 0),
        "trend_ema": lambda f: _sign((f["ema_9"] - f["ema_21"]) if (f["ema_9"] and f["ema_21"]) else None),
        "vwap": lambda f: _sign((f["close"] - f["vwap"]) if f["vwap"] else None),
        # structural position: continuation (push through edge) vs reversal (fade the edge)
        "pos_continuation": lambda f: {"near_high": 1, "near_low": -1}.get(f["struct_position"], 0),
        "pos_reversal": lambda f: {"near_high": -1, "near_low": 1}.get(f["struct_position"], 0),
    }


def _agreement(feats: dict, preds: dict[str, Callable[[dict], int]], names: list[str]) -> int:
    votes = [preds[n](feats) for n in names]
    s = sum(votes)
    return 1 if s > 0 else -1 if s < 0 else 0


def _load_days_from_mongo() -> dict[str, list[dict[str, Any]]]:
    from pymongo import MongoClient

    host = os.getenv("MONGO_HOST", "mongo")
    db = os.getenv("MONGO_DB", "trading_ai")
    coll = MongoClient(f"mongodb://{host}:27017")[db][os.getenv("BIGMOVE_SOURCE_COLL", "phase1_market_snapshots")]
    days = sorted(str(d) for d in coll.distinct("trade_date_ist") if d)
    out: dict[str, list[dict[str, Any]]] = {}
    for day in days:
        rows = []
        for d in coll.find({"trade_date_ist": day}).sort("timestamp", 1):
            s = (d.get("payload") or {}).get("snapshot") or {}
            f = s.get("futures_bar") or {}
            fd = s.get("futures_derived") or {}
            ca = s.get("chain_aggregates") or {}
            rows.append({
                "c": f.get("fut_close"), "h": f.get("fut_high"), "l": f.get("fut_low"),
                "ovol": (ca.get("total_ce_volume") or 0) + (ca.get("total_pe_volume") or 0),
                "ooi": (ca.get("total_ce_oi") or 0) + (ca.get("total_pe_oi") or 0),
                "vwap": fd.get("vwap"), "ema_9": fd.get("ema_9"), "ema_21": fd.get("ema_21"),
                "fut_return_5m": fd.get("fut_return_5m"),
            })
        out[day] = rows
    return out


def _features_and_outcome(bars: list[dict[str, Any]], i: int) -> Optional[dict]:
    b = bars[i]
    if b["c"] is None or i < WARMUP:
        return None
    win = [x for x in bars[i + 1:i + 1 + HORIZON] if x["h"] is not None and x["l"] is not None]
    if not win:
        return None
    close = float(b["c"])
    up = max(float(x["h"]) for x in win) - close
    down = close - min(float(x["l"]) for x in win)
    move = max(up, down)
    signed = up if up >= down else -down

    H = [x["h"] for x in bars[i - BUILD_WINDOW:i]]; L = [x["l"] for x in bars[i - BUILD_WINDOW:i]]
    C = [x["c"] for x in bars[i - BUILD_WINDOW - 1:i]]
    Hb = [x["h"] for x in bars[i - WARMUP + 1:i - BUILD_WINDOW]]; Lb = [x["l"] for x in bars[i - WARMUP + 1:i - BUILD_WINDOW]]
    Cb = [x["c"] for x in bars[i - WARMUP:i - BUILD_WINDOW]]
    if any(v is None for v in H + L + C + Hb + Lb + Cb):
        return None
    atr_build = _atr(H, L, C); atr_base = _atr(Hb, Lb, Cb)
    compression = bool(atr_base and atr_build < COMPRESS_RATIO * atr_base)
    oi_build = bool(b["ooi"] and bars[i - BUILD_WINDOW]["ooi"] and b["ooi"] > bars[i - BUILD_WINDOW]["ooi"] * OI_BUILD)
    loaded = compression and oi_build

    struct = _structure_for_bar(bars, i)
    return {
        "close": close, "ret_1m_pt": close - float(bars[i - 1]["c"]) if bars[i - 1]["c"] else 0.0,
        "fut_return_5m": b.get("fut_return_5m"), "vwap": b.get("vwap"),
        "ema_9": b.get("ema_9"), "ema_21": b.get("ema_21"),
        "struct_breakout": struct["struct_breakout"], "struct_trend": struct["struct_trend"],
        "struct_position": struct["struct_position"],
        "loaded": loaded, "move": move, "actual_side": _sign(signed),
    }


def _score(samples: list[dict], preds: dict[str, Callable[[dict], int]]) -> list[tuple[str, int, float]]:
    rows = []
    names = list(preds)
    for name in names:
        fn = preds[name]
        dec = [s for s in samples if fn(s) != 0]
        if not dec:
            rows.append((name, 0, 0.0)); continue
        acc = sum(1 for s in dec if fn(s) == s["actual_side"]) / len(dec)
        rows.append((name, len(dec), acc))
    # agreement of the 3 cleanest trend/breakout signals
    trio = ["momentum_5m", "breakout", "trend_ema"]
    dec = [s for s in samples if _agreement(s, preds, trio) != 0]
    if dec:
        acc = sum(1 for s in dec if _agreement(s, preds, trio) == s["actual_side"]) / len(dec)
        rows.append(("AGREE(mom5,breakout,ema)", len(dec), acc))
    return rows


def _print(title: str, samples: list[dict], preds) -> None:
    base_up = sum(1 for s in samples if s["actual_side"] == 1) / len(samples) if samples else 0.0
    print(f"\n=== {title} | n={len(samples)} | base up-rate={base_up:.1%} ===")
    print(f"{'predictor':>26} {'coverage':>9} {'accuracy':>9}")
    for name, cov, acc in _score(samples, preds):
        flag = "  <-- >0.55" if acc > 0.55 and cov >= 20 else ""
        print(f"{name:>26} {cov:>9} {acc:>8.1%}{flag}")


def main() -> None:
    days_bars = _load_days_from_mongo()
    preds = _predictors()
    all_samples, moved, loaded_moved = [], [], []
    for bars in days_bars.values():
        for i in range(len(bars)):
            f = _features_and_outcome(bars, i)
            if f is None:
                continue
            all_samples.append(f)
            if f["move"] >= MIN_MOVE_PT:
                moved.append(f)
                if f["loaded"]:
                    loaded_moved.append(f)

    print(f"Direction research | days={len(days_bars)} | bars_scored={len(all_samples)} "
          f"| moved>={MIN_MOVE_PT:.0f}pt={len(moved)} | loaded&moved={len(loaded_moved)}")
    _print(f"ALL bars with a real move (>= {MIN_MOVE_PT:.0f}pt)", moved, preds)
    _print("LOADED bars with a real move (the bars we'd trade)", loaded_moved, preds)
    print("\nRead: accuracy = P(predicted side == realised side) on decisive bars. "
          ">0.55 with coverage is a candidate; otherwise direction stays UNKNOWN (abstain).")


if __name__ == "__main__":
    main()
