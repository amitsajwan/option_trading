from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from .entry_quality_config import DEFAULT_MODEL_ROOT


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_strategy_concentration(summary_json_path: Path) -> tuple[Optional[str], Optional[float]]:
    if not summary_json_path.exists():
        return None, None
    payload = json.loads(summary_json_path.read_text(encoding="utf-8"))
    strategy_csv = payload.get("outputs", {}).get("ml_by_strategy_csv")
    if not strategy_csv:
        return None, None
    strategy_path = Path(str(strategy_csv))
    if not strategy_path.exists():
        return None, None
    try:
        strategies = pd.read_csv(strategy_path)
    except Exception:
        return None, None
    if strategies.empty or "total_capital_pnl_pct" not in strategies.columns:
        return None, None
    strategies["total_capital_pnl_pct"] = pd.to_numeric(strategies["total_capital_pnl_pct"], errors="coerce")
    strategies = strategies.dropna(subset=["total_capital_pnl_pct"])
    if strategies.empty:
        return None, None
    strategies["abs_pnl_share_component"] = strategies["total_capital_pnl_pct"].abs()
    total_abs = float(strategies["abs_pnl_share_component"].sum())
    if total_abs <= 0.0:
        return None, None
    top = strategies.sort_values("abs_pnl_share_component", ascending=False, kind="stable").iloc[0]
    top_share = float(top["abs_pnl_share_component"]) / total_abs
    return str(top.get("entry_strategy") or ""), top_share


def _deterministic_trade_count(summary_json_path: Path) -> Optional[float]:
    if not summary_json_path.exists():
        return None
    payload = json.loads(summary_json_path.read_text(encoding="utf-8"))
    return _safe_float(payload.get("deterministic", {}).get("trades"))


def _evaluate_acceptance(
    row: pd.Series,
    *,
    min_trades: int,
    max_drawdown_pct: float,
    drawdown_multiple: float,
    min_trade_ratio: float,
    max_single_strategy_return_share: float,
    require_positive_return: bool,
    min_outperformance_pct: float,
) -> dict[str, object]:
    ml_return = _safe_float(row.get("ml_capital_return_pct"))
    det_return = _safe_float(row.get("det_capital_return_pct"))
    ml_drawdown = _safe_float(row.get("ml_max_drawdown_pct"))
    det_drawdown = _safe_float(row.get("det_max_drawdown_pct"))
    ml_trades = _safe_float(row.get("ml_trades"))
    summary_json_path = Path(str(row.get("summary_json") or ""))
    det_trades = _safe_float(row.get("det_trades"))
    if det_trades is None:
        det_trades = _deterministic_trade_count(summary_json_path)
    top_strategy, top_share = _compute_strategy_concentration(summary_json_path)

    min_trades_gate = ml_trades is not None and ml_trades >= int(min_trades)
    max_drawdown_gate = (
        ml_drawdown is not None and ml_drawdown >= float(max_drawdown_pct)
    )

    return_gate = (
        ml_return is not None
        and det_return is not None
        and ml_return > (det_return + float(min_outperformance_pct))
    )
    positive_return_gate = (
        (ml_return is not None and ml_return > 0.0) if bool(require_positive_return) else True
    )
    if det_drawdown is None or ml_drawdown is None:
        drawdown_gate = False
    elif det_drawdown < 0:
        drawdown_gate = ml_drawdown >= det_drawdown * float(drawdown_multiple)
    else:
        drawdown_gate = ml_drawdown >= det_drawdown
    trade_count_gate = (
        ml_trades is not None
        and det_trades is not None
        and ml_trades >= det_trades * float(min_trade_ratio)
    )
    strategy_diversification_gate = (
        top_share is None or top_share <= float(max_single_strategy_return_share)
    )

    accepted = all(
        [
            min_trades_gate,
            max_drawdown_gate,
            return_gate,
            positive_return_gate,
            drawdown_gate,
            trade_count_gate,
            strategy_diversification_gate,
        ]
    )
    reasons: list[str] = []
    if not min_trades_gate:
        reasons.append("min_trades_gate_failed")
    if not max_drawdown_gate:
        reasons.append("max_drawdown_gate_failed")
    if not return_gate:
        reasons.append("return_not_better_than_baseline")
    if not positive_return_gate:
        reasons.append("positive_return_gate_failed")
    if not drawdown_gate:
        reasons.append("drawdown_gate_failed")
    if not trade_count_gate:
        reasons.append("trade_count_gate_failed")
    if not strategy_diversification_gate:
        reasons.append("strategy_concentration_gate_failed")
    return {
        "accepted": accepted,
        "policy_gate_evaluated": True,
        "min_trades_gate": min_trades_gate,
        "max_drawdown_gate": max_drawdown_gate,
        "return_gate": return_gate,
        "drawdown_gate": drawdown_gate,
        "trade_count_gate": trade_count_gate,
        "strategy_diversification_gate": strategy_diversification_gate,
        "positive_return_gate": positive_return_gate,
        "top_strategy": top_strategy,
        "top_strategy_return_share": top_share,
        "rejection_reasons": reasons,
    }


def select_champions(
    *,
    evaluation_registry_path: Path,
    output_dir: Path,
    max_champions: int = 5,
    min_trades: int = 10,
    max_drawdown_pct: float = 0.0,
    drawdown_multiple: float = 1.15,
    min_trade_ratio: float = 0.60,
    max_single_strategy_return_share: float = 0.70,
    require_positive_return: bool = True,
    min_outperformance_pct: float = 0.005,
    allow_single_exception: bool = False,
    exception_min_trades: int = 30,
    exception_reason_out: Optional[Path] = None,
) -> dict[str, object]:
    df = pd.read_csv(evaluation_registry_path)
    if df.empty:
        raise ValueError("evaluation registry is empty")

    scored = df.copy()
    scored["ml_trades"] = pd.to_numeric(scored["ml_trades"], errors="coerce")
    scored["ml_max_drawdown_pct"] = pd.to_numeric(scored["ml_max_drawdown_pct"], errors="coerce")

    gate_rows = []
    for _, row in scored.iterrows():
        gate_rows.append(
            _evaluate_acceptance(
                row,
                min_trades=int(min_trades),
                max_drawdown_pct=float(max_drawdown_pct),
                drawdown_multiple=float(drawdown_multiple),
                min_trade_ratio=float(min_trade_ratio),
                max_single_strategy_return_share=float(max_single_strategy_return_share),
                require_positive_return=bool(require_positive_return),
                min_outperformance_pct=float(min_outperformance_pct),
            )
        )
    if gate_rows:
        gate_df = pd.DataFrame(gate_rows)
        scored = pd.concat([scored.reset_index(drop=True), gate_df], axis=1)
    else:
        scored["accepted"] = False
        scored["rejection_reasons"] = [[] for _ in range(len(scored))]

    accepted_df = scored[scored["accepted"] == True].copy()
    rejected_df = scored[scored["accepted"] != True].copy()

    accepted_df = accepted_df.sort_values(
        ["ml_capital_return_pct", "ml_max_drawdown_pct", "ml_profit_factor", "ml_trades"],
        ascending=[False, False, False, False],
        kind="stable",
    )
    champions = accepted_df.head(int(max_champions)).copy()
    if champions.empty:
        champions["promotion_mode"] = pd.Series(dtype="string")
    else:
        champions["promotion_mode"] = "hard_gate"
    if champions.empty:
        champions["promotion_reason"] = pd.Series(dtype="string")
    else:
        champions["promotion_reason"] = champions.apply(
            lambda row: (
                f"replay_return={row['ml_capital_return_pct']:.6f}, "
                f"drawdown={row['ml_max_drawdown_pct']:.6f}, "
                f"pf={row['ml_profit_factor']:.6f}, "
                f"trades={int(row['ml_trades'])}"
            ),
            axis=1,
        )

    exception_record: Optional[dict[str, object]] = None
    if bool(allow_single_exception):
        exception_pool = rejected_df.copy()
        if not exception_pool.empty:
            exception_pool["ml_capital_return_pct"] = pd.to_numeric(exception_pool["ml_capital_return_pct"], errors="coerce")
            exception_pool["det_capital_return_pct"] = pd.to_numeric(exception_pool["det_capital_return_pct"], errors="coerce")
            exception_pool["ml_trades"] = pd.to_numeric(exception_pool["ml_trades"], errors="coerce")
            if "drawdown_gate" in exception_pool.columns:
                exception_pool["drawdown_gate"] = exception_pool["drawdown_gate"].fillna(False).astype(bool)
            else:
                exception_pool["drawdown_gate"] = False
            exception_pool = exception_pool[
                (exception_pool["ml_capital_return_pct"] > exception_pool["det_capital_return_pct"])
                & (
                    (exception_pool["ml_capital_return_pct"] > 0.0)
                    if bool(require_positive_return)
                    else True
                )
                & (
                    (exception_pool["ml_capital_return_pct"] > (exception_pool["det_capital_return_pct"] + float(min_outperformance_pct)))
                )
                & (exception_pool["ml_trades"] >= int(exception_min_trades))
                & (exception_pool["drawdown_gate"] == True)
            ].copy()
            if not exception_pool.empty and len(champions) < int(max_champions):
                exception_pool = exception_pool.sort_values(
                    ["ml_capital_return_pct", "ml_max_drawdown_pct", "ml_profit_factor", "ml_trades"],
                    ascending=[False, False, False, False],
                    kind="stable",
                )
                exception_row = exception_pool.iloc[[0]].copy()
                exception_row["promotion_mode"] = "controlled_exception"
                exception_row["promotion_reason"] = exception_row.apply(
                    lambda row: (
                        f"controlled_exception: replay_return={row['ml_capital_return_pct']:.6f}, "
                        f"drawdown={row['ml_max_drawdown_pct']:.6f}, "
                        f"pf={row['ml_profit_factor']:.6f}, "
                        f"trades={int(row['ml_trades'])}"
                    ),
                    axis=1,
                )
                champions = pd.concat([champions, exception_row], ignore_index=True)
                rejected_df = rejected_df[rejected_df["experiment_id"] != exception_row.iloc[0]["experiment_id"]].copy()
                signer = str(os.getenv("USER") or os.getenv("USERNAME") or "").strip() or "unknown"
                exception_record = {
                    "used": True,
                    "experiment_id": str(exception_row.iloc[0]["experiment_id"]),
                    "base_experiment_key": str(exception_row.iloc[0].get("base_experiment_key") or ""),
                    "threshold_policy_id": str(exception_row.iloc[0].get("threshold_policy_id") or ""),
                    "reason": "Single controlled exception applied: return beat deterministic, minimum trades met, drawdown gate passed.",
                    "conditions": {
                        "ml_return": float(exception_row.iloc[0]["ml_capital_return_pct"]),
                        "det_return": float(exception_row.iloc[0]["det_capital_return_pct"]),
                        "ml_trades": int(float(exception_row.iloc[0]["ml_trades"])),
                        "exception_min_trades": int(exception_min_trades),
                        "drawdown_gate": bool(exception_row.iloc[0]["drawdown_gate"]),
                    },
                    "signed_by": signer,
                    "signed_at_utc": _utc_now(),
                }
                if exception_reason_out is None:
                    raise ValueError(
                        "controlled exception selected but --exception-reason-out was not provided"
                    )
                exception_reason_out.parent.mkdir(parents=True, exist_ok=True)
                exception_reason_out.write_text(json.dumps(exception_record, indent=2), encoding="utf-8")
            elif not exception_pool.empty:
                exception_record = {
                    "used": False,
                    "reason": "Eligible controlled exception candidate exists but champion cap already reached.",
                }
            else:
                exception_record = {"used": False, "reason": "No rejected candidate met controlled exception conditions."}
        else:
            exception_record = {"used": False, "reason": "No rejected candidates available for controlled exception."}

    output_dir.mkdir(parents=True, exist_ok=True)
    champions.to_csv(output_dir / "champion_registry.csv", index=False)
    rejected_df.to_csv(output_dir / "rejected_candidates.csv", index=False)
    payload = {
        "created_at_utc": _utc_now(),
        "source_registry": str(evaluation_registry_path).replace("\\", "/"),
        "max_champions": int(max_champions),
        "min_trades": int(min_trades),
        "max_drawdown_pct_gate": float(max_drawdown_pct),
        "drawdown_multiple_gate": float(drawdown_multiple),
        "min_trade_ratio_gate": float(min_trade_ratio),
        "max_single_strategy_return_share_gate": float(max_single_strategy_return_share),
        "require_positive_return_gate": bool(require_positive_return),
        "min_outperformance_pct_gate": float(min_outperformance_pct),
        "allow_single_exception": bool(allow_single_exception),
        "exception_min_trades": int(exception_min_trades),
        "exception_reason_out": (str(exception_reason_out).replace("\\", "/") if exception_reason_out else None),
        "controlled_exception": exception_record,
        "champions": champions.to_dict(orient="records"),
        "rejected_candidates": rejected_df.to_dict(orient="records"),
    }
    (output_dir / "champion_registry.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Select champion entry-quality models from replay evaluation outputs.")
    parser.add_argument("--evaluation-registry", default=str(DEFAULT_MODEL_ROOT / "entry_quality_replay_eval" / "evaluation_registry.csv"))
    parser.add_argument("--output-dir", default=str(DEFAULT_MODEL_ROOT / "entry_quality_champions"))
    parser.add_argument("--max-champions", type=int, default=5)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--max-drawdown-pct", type=float, default=-0.50)
    parser.add_argument("--drawdown-multiple", type=float, default=1.15)
    parser.add_argument("--min-trade-ratio", type=float, default=0.60)
    parser.add_argument("--max-single-strategy-return-share", type=float, default=0.70)
    parser.add_argument("--require-positive-return", dest="require_positive_return", action="store_true")
    parser.add_argument("--allow-non-positive-return", dest="require_positive_return", action="store_false")
    parser.set_defaults(require_positive_return=None)
    parser.add_argument("--min-outperformance-pct", type=float, default=0.005)
    parser.add_argument("--allow-single-exception", action="store_true")
    parser.add_argument("--exception-min-trades", type=int, default=30)
    parser.add_argument("--exception-reason-out", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    require_positive_return = (
        True if args.require_positive_return is None else bool(args.require_positive_return)
    )
    payload = select_champions(
        evaluation_registry_path=Path(args.evaluation_registry),
        output_dir=Path(args.output_dir),
        max_champions=int(args.max_champions),
        min_trades=int(args.min_trades),
        max_drawdown_pct=float(args.max_drawdown_pct),
        drawdown_multiple=float(args.drawdown_multiple),
        min_trade_ratio=float(args.min_trade_ratio),
        max_single_strategy_return_share=float(args.max_single_strategy_return_share),
        require_positive_return=bool(require_positive_return),
        min_outperformance_pct=float(args.min_outperformance_pct),
        allow_single_exception=bool(args.allow_single_exception),
        exception_min_trades=int(args.exception_min_trades),
        exception_reason_out=(Path(args.exception_reason_out) if args.exception_reason_out else None),
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
