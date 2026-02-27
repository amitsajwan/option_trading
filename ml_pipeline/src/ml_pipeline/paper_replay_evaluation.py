import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .backtest_engine import compute_trade_outcome_from_row
from .fill_model import FillModelConfig, config_to_dict as fill_config_to_dict, estimate_slippage_return


def _load_json(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_decisions_jsonl(path: Path) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "action", "ce_prob", "pe_prob"])
    df = pd.DataFrame(rows)
    if "timestamp" not in df.columns:
        df["timestamp"] = pd.NaT
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for col in ("ce_prob", "pe_prob"):
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "action" not in df.columns:
        df["action"] = "HOLD"
    df["action"] = df["action"].astype(str)
    return df.sort_values("timestamp").reset_index(drop=True)


def _resolve_thresholds(threshold_payload: Dict[str, object]) -> Tuple[float, float]:
    ce = threshold_payload.get("ce", {}).get("selected_threshold")
    pe = threshold_payload.get("pe", {}).get("selected_threshold")
    if ce is None or pe is None:
        raise ValueError("threshold report missing ce/pe selected_threshold")
    return float(ce), float(pe)


def _profile_from_t19(t19_payload: Optional[Dict[str, object]], default_cost: float) -> Dict[str, object]:
    if not t19_payload:
        return {
            "name": "fallback_fixed_horizon",
            "execution_mode": "fixed_horizon",
            "intrabar_tie_break": "sl",
            "slippage_per_trade": 0.0,
            "forced_eod_exit_time": "15:24",
            "cost_per_trade": float(default_cost),
        }
    best = t19_payload.get("best_profile")
    if isinstance(best, dict):
        return {
            "name": str(best.get("name", "best_profile")),
            "execution_mode": str(best.get("execution_mode", "fixed_horizon")),
            "intrabar_tie_break": str(best.get("intrabar_tie_break", "sl")),
            "slippage_per_trade": float(best.get("slippage_per_trade", 0.0)),
            "forced_eod_exit_time": str(best.get("forced_eod_exit_time", "15:24")),
            "cost_per_trade": float(best.get("cost_per_trade", default_cost)),
        }
    return {
        "name": "fallback_fixed_horizon",
        "execution_mode": "fixed_horizon",
        "intrabar_tie_break": "sl",
        "slippage_per_trade": 0.0,
        "forced_eod_exit_time": "15:24",
        "cost_per_trade": float(default_cost),
    }


def evaluate_replay(
    decisions_df: pd.DataFrame,
    labeled_df: pd.DataFrame,
    ce_threshold: float,
    pe_threshold: float,
    profile: Dict[str, object],
    fill_model_config: FillModelConfig,
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    labeled = labeled_df.copy()
    labeled["timestamp"] = pd.to_datetime(labeled["timestamp"], errors="coerce")
    labeled = labeled.dropna(subset=["timestamp"]).sort_values("timestamp")
    lookup = labeled.set_index("timestamp", drop=False)

    trades: List[Dict[str, object]] = []
    matched = 0
    unmatched = 0
    holds = 0
    buys = 0

    for row in decisions_df.itertuples(index=False):
        ts = pd.Timestamp(row.timestamp)
        action = str(row.action)
        if action not in {"BUY_CE", "BUY_PE"}:
            holds += 1
            continue
        buys += 1
        if ts not in lookup.index:
            unmatched += 1
            continue
        market_row = lookup.loc[ts]
        if isinstance(market_row, pd.DataFrame):
            market_row = market_row.iloc[0]
        side = "CE" if action == "BUY_CE" else "PE"
        horizon = int(market_row["label_horizon_minutes"])
        selected_prob = float(row.ce_prob) if side == "CE" else float(row.pe_prob)
        threshold = float(ce_threshold) if side == "CE" else float(pe_threshold)
        model_slippage = estimate_slippage_return(market_row, side=side, config=fill_model_config)

        outcome = compute_trade_outcome_from_row(
            row=market_row,
            side=side,
            decision_ts=ts,
            horizon_minutes=horizon,
            selected_prob=selected_prob,
            selected_threshold=threshold,
            ce_prob=float(row.ce_prob),
            pe_prob=float(row.pe_prob),
            cost_per_trade=float(profile["cost_per_trade"]),
            slippage_per_trade=float(profile["slippage_per_trade"]),
            slippage_model_component=float(model_slippage),
            execution_mode=str(profile["execution_mode"]),
            intrabar_tie_break=str(profile["intrabar_tie_break"]),
            forced_eod_exit_time=str(profile["forced_eod_exit_time"]),
        )

        trades.append(
            {
                "decision_timestamp": ts,
                "trade_date": str(market_row["trade_date"]),
                "action": action,
                "side": side,
                "ce_prob": float(row.ce_prob),
                "pe_prob": float(row.pe_prob),
                "ce_threshold": float(ce_threshold),
                "pe_threshold": float(pe_threshold),
                "profile_name": str(profile["name"]),
                **outcome,
            }
        )
        matched += 1

    trade_df = pd.DataFrame(trades)
    if len(trade_df):
        trade_df = trade_df.sort_values("decision_timestamp").reset_index(drop=True)
        trade_df["cum_net_return"] = trade_df["net_return"].cumsum()
    else:
        trade_df["cum_net_return"] = pd.Series(dtype=float)

    exit_counts = (
        {str(k): int(v) for k, v in trade_df["exit_reason"].value_counts().to_dict().items()}
        if len(trade_df) and "exit_reason" in trade_df.columns
        else {}
    )
    side_counts = (
        {str(k): int(v) for k, v in trade_df["side"].value_counts().to_dict().items()}
        if len(trade_df) and "side" in trade_df.columns
        else {}
    )
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "fill_model": fill_config_to_dict(fill_model_config),
        "decisions_total": int(len(decisions_df)),
        "buy_decisions_total": int(buys),
        "hold_decisions_total": int(holds),
        "matched_trades": int(matched),
        "unmatched_buy_decisions": int(unmatched),
        "match_rate": float(matched / buys) if buys else 0.0,
        "trades_total": int(len(trade_df)),
        "net_return_sum": float(trade_df["net_return"].sum()) if len(trade_df) else 0.0,
        "mean_net_return_per_trade": float(trade_df["net_return"].mean()) if len(trade_df) else 0.0,
        "win_rate": float((trade_df["net_return"] > 0).mean()) if len(trade_df) else 0.0,
        "exit_reason_counts": exit_counts,
        "side_counts": side_counts,
    }
    return trade_df, report


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate paper replay decisions against realized labeled outcomes")
    parser.add_argument("--decisions-jsonl", default="ml_pipeline/artifacts/t11_paper_decisions.jsonl")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t05_labeled_features.parquet")
    parser.add_argument("--threshold-report", default="ml_pipeline/artifacts/t08_threshold_report.json")
    parser.add_argument("--t19-report", default="ml_pipeline/artifacts/t19_strategy_comparison_v2_report.json")
    parser.add_argument("--trades-out", default="ml_pipeline/artifacts/t21_replay_evaluation_trades.parquet")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t21_replay_evaluation_report.json")
    parser.add_argument("--fill-model", default="constant", choices=["constant", "spread_fraction", "liquidity_adjusted"])
    parser.add_argument("--fill-constant", type=float, default=0.0)
    parser.add_argument("--fill-spread-fraction", type=float, default=0.5)
    parser.add_argument("--fill-volume-impact", type=float, default=0.02)
    parser.add_argument("--fill-min", type=float, default=0.0)
    parser.add_argument("--fill-max", type=float, default=0.01)
    args = parser.parse_args(list(argv) if argv is not None else None)

    decisions_path = Path(args.decisions_jsonl)
    labeled_path = Path(args.labeled_data)
    threshold_path = Path(args.threshold_report)
    t19_path = Path(args.t19_report)
    if not decisions_path.exists():
        print(f"ERROR: decisions file not found: {decisions_path}")
        return 2
    if not labeled_path.exists():
        print(f"ERROR: labeled data not found: {labeled_path}")
        return 2
    if not threshold_path.exists():
        print(f"ERROR: threshold report not found: {threshold_path}")
        return 2

    decisions = load_decisions_jsonl(decisions_path)
    labeled = pd.read_parquet(labeled_path)
    threshold_payload = _load_json(threshold_path)
    ce_thr, pe_thr = _resolve_thresholds(threshold_payload)
    default_cost = float((threshold_payload.get("decision_config") or {}).get("cost_per_trade", 0.0006))
    t19_payload = _load_json(t19_path) if t19_path.exists() else None
    profile = _profile_from_t19(t19_payload=t19_payload, default_cost=default_cost)

    fill_cfg = FillModelConfig(
        model=str(args.fill_model),
        constant_slippage=float(args.fill_constant),
        spread_fraction=float(args.fill_spread_fraction),
        volume_impact_coeff=float(args.fill_volume_impact),
        min_slippage=float(args.fill_min),
        max_slippage=float(args.fill_max),
    )
    trades, report = evaluate_replay(
        decisions_df=decisions,
        labeled_df=labeled,
        ce_threshold=ce_thr,
        pe_threshold=pe_thr,
        profile=profile,
        fill_model_config=fill_cfg,
    )

    trades_out = Path(args.trades_out)
    report_out = Path(args.report_out)
    trades_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(trades_out, index=False)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Decisions total: {report['decisions_total']}")
    print(f"Matched trades: {report['matched_trades']}")
    print(f"Net return sum: {report['net_return_sum']}")
    print(f"Trades: {trades_out}")
    print(f"Report: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
