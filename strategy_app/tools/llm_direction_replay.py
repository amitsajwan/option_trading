r"""Offline LLM direction-advisor replay — does an LLM beat our 56% direction coin-flip?

Premise (project memory): ENTRY is solved; DIRECTION is the entire problem (~56% right-side
on big moves). This harness measures, on HISTORICAL data we already know the outcome of,
whether handing an LLM the full grounded picture and asking "CE or PE?" beats that baseline.

It is a MEASUREMENT tool, not a live path. The prompt / provider / call logic live in
``strategy_app.brain.direction_advisor`` (shared with the live shadow hook). For each
selected big-move bar this tool:
  1. computes the realised forward direction over --horizon-min  (ground truth),
  2. builds the curated fact bundle (advisor.build_facts),
  3. asks the LLM (advisor.ask_direction) for CE|PE|abstain + confidence,
  4. scores the pick against truth and against the vwap-side / momentum baselines.

NOTE: offline cannot include web_context (no as-of-date historical news) — that grounding,
the most promising edge, is live-shadow-only. This run measures the STRUCTURAL floor.

Population = bars where a real move happened (|forward return| >= --move-threshold).

Usage (PowerShell):
  $env:GROQ_API_KEY="..."
  python -m strategy_app.tools.llm_direction_replay --provider groq `
      --days 15 --horizon-min 15 --move-threshold 0.0025 --max-calls 150 `
      --out c:\tmp\llm_dir_replay.json
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

try:
    from ..brain.direction_advisor import ask_direction, build_facts, resolve_provider, PROVIDERS
except ImportError:  # pragma: no cover - script-mode fallback
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from strategy_app.brain.direction_advisor import (  # type: ignore
        ask_direction, build_facts, resolve_provider, PROVIDERS,
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("llm_direction_replay")

_DEFAULT_DATA = ".data/ml_pipeline/parquet_data/snapshots_ml_flat_v2"


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


@dataclass
class BarSample:
    trade_date: str
    timestamp: str
    facts: dict[str, Any]
    truth: str            # CE if underlying rose, PE if fell
    fwd_return: float
    vwap_side: str
    mom_side: str


def _side(x: Optional[float]) -> str:
    if x is None or x == 0:
        return "none"
    return "CE" if x > 0 else "PE"


def select_bars(data_dir: str, *, days: int, horizon_min: int, move_threshold: float,
                start_min: int = 575, end_min: int = 905, seed: int = 7) -> list[BarSample]:
    """Pick big-move bars across the most recent `days` daily parquets."""
    files = sorted(glob.glob(os.path.join(data_dir, "**", "*.parquet"), recursive=True))
    if not files:
        raise SystemExit(f"no parquet files under {data_dir}")
    files = files[-days:]
    samples: list[BarSample] = []
    for f in files:
        df = pd.read_parquet(f)
        if "timestamp" not in df or "px_fut_close" not in df:
            continue
        df = df.sort_values("timestamp").reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        fwd = pd.merge_asof(
            df[["timestamp"]].assign(target=df["timestamp"] + pd.Timedelta(minutes=horizon_min)),
            df[["timestamp", "px_fut_close"]].rename(columns={"timestamp": "t2", "px_fut_close": "fwd_close"}),
            left_on="target", right_on="t2", direction="nearest",
            tolerance=pd.Timedelta(minutes=3),
        )
        df["fwd_close"] = fwd["fwd_close"].values
        for _, row in df.iterrows():
            mod = _num(row.get("time_minute_of_day"))
            if mod is None or mod < start_min or mod > end_min:
                continue
            px, fwd_px = _num(row.get("px_fut_close")), _num(row.get("fwd_close"))
            if px is None or fwd_px is None or px == 0:
                continue
            ret = (fwd_px - px) / px
            if abs(ret) < move_threshold:
                continue
            samples.append(BarSample(
                trade_date=str(row.get("trade_date") or "")[:10],
                timestamp=str(row["timestamp"]),
                facts=build_facts(row),
                truth="CE" if ret > 0 else "PE",
                fwd_return=round(ret, 5),
                vwap_side=_side(_num(row.get("vwap_distance"))),
                mom_side=_side(_num(row.get("ret_5m"))),
            ))
    random.Random(seed).shuffle(samples)
    return samples


def _acc(picks: list[tuple[str, str]]) -> dict[str, Any]:
    graded = [(p, t) for p, t in picks if p in ("CE", "PE")]
    if not graded:
        return {"n": 0, "accuracy": None, "coverage": 0.0}
    correct = sum(1 for p, t in graded if p == t)
    return {"n": len(graded), "accuracy": round(correct / len(graded), 4),
            "coverage": round(len(graded) / len(picks), 4)}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=list(PROVIDERS), default="groq")
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--data-dir", default=_DEFAULT_DATA)
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--horizon-min", type=int, default=15)
    ap.add_argument("--move-threshold", type=float, default=0.0025)
    ap.add_argument("--max-calls", type=int, default=150)
    ap.add_argument("--timeout-s", type=float, default=25.0)
    ap.add_argument("--sleep-ms", type=int, default=1200)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true", help="select bars + baselines, no LLM")
    args = ap.parse_args(argv)

    base_url, default_model, key_envs = resolve_provider(args.provider)
    model = args.model or default_model
    api_key = args.api_key or next((os.getenv(k, "").strip() for k in key_envs if os.getenv(k)), "")
    if not args.dry_run and not api_key:
        raise SystemExit(f"no API key — set one of {key_envs} or pass --api-key")

    samples = select_bars(args.data_dir, days=args.days, horizon_min=args.horizon_min,
                          move_threshold=args.move_threshold)
    logger.info("selected %d big-move bars from last %d days (horizon=%dm, thr=%.4f)",
                len(samples), args.days, args.horizon_min, args.move_threshold)
    if not samples:
        raise SystemExit("no qualifying bars — loosen --move-threshold or raise --days")
    if len(samples) > args.max_calls:
        step = len(samples) / args.max_calls
        samples = [samples[int(i * step)] for i in range(args.max_calls)]
        logger.info("capped to %d bars (--max-calls)", len(samples))

    vwap_acc = _acc([(s.vwap_side, s.truth) for s in samples])
    mom_acc = _acc([(s.mom_side, s.truth) for s in samples])
    logger.info("baseline vwap-side: %s | momentum-5m: %s", vwap_acc, mom_acc)

    if args.dry_run:
        print(json.dumps({"n_bars": len(samples), "baseline_vwap": vwap_acc,
                          "baseline_momentum": mom_acc,
                          "ce_truth_frac": round(sum(s.truth == "CE" for s in samples) / len(samples), 3)},
                         indent=2))
        return 0

    results = []
    errors = 0
    t0 = time.time()
    for i, s in enumerate(samples, 1):
        v = ask_direction(s.facts, base_url=base_url, api_key=api_key, model=model,
                          timeout_s=args.timeout_s)
        if v.error:
            errors += 1
        results.append({"trade_date": s.trade_date, "timestamp": s.timestamp, "truth": s.truth,
                        "fwd_return": s.fwd_return, "vwap_side": s.vwap_side,
                        "llm": v.as_dict(), "facts": s.facts})
        if i % 25 == 0:
            logger.info("[%d/%d] %.1fs elapsed", i, len(samples), time.time() - t0)
        if args.sleep_ms:
            time.sleep(args.sleep_ms / 1000.0)

    committed = [r for r in results if r["llm"]["direction"] in ("CE", "PE")]
    hi = [r for r in committed if r["llm"]["confidence"] >= 0.60]
    summary = {
        "provider": args.provider, "model": model,
        "config": {"days": args.days, "horizon_min": args.horizon_min,
                   "move_threshold": args.move_threshold, "n_bars": len(results),
                   "llm_errors": errors},
        "ce_truth_frac": round(sum(r["truth"] == "CE" for r in results) / len(results), 4),
        "baseline_vwap_side": vwap_acc,
        "baseline_momentum_5m": mom_acc,
        "llm_all_bars": _acc([(r["llm"]["direction"], r["truth"]) for r in results]),
        "llm_committed_only": _acc([(r["llm"]["direction"], r["truth"]) for r in committed]),
        "llm_high_conf_ge_0.60": _acc([(r["llm"]["direction"], r["truth"]) for r in hi]),
        "vwap_on_llm_committed_subset": _acc([(r["vwap_side"], r["truth"]) for r in committed]),
    }
    print("\n" + json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "results": results}, fh, indent=2)
        logger.info("wrote %d per-bar results -> %s", len(results), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
