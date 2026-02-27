import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DynamicExitPolicyConfig:
    stop_loss_pct: float = 0.12
    take_profit_pct: float = 0.24
    enable_trailing_stop: bool = False
    trailing_stop_pct: Optional[float] = None
    move_to_break_even_at_profit_pct: Optional[float] = None
    allow_hold_extension: bool = False
    max_hold_extension_minutes: int = 0
    extension_min_model_prob: Optional[float] = None
    intrabar_tie_break: str = "sl"  # sl|tp


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def validate_policy_config(cfg: DynamicExitPolicyConfig) -> List[str]:
    errors: List[str] = []
    if cfg.stop_loss_pct <= 0.0 or cfg.stop_loss_pct >= 1.0:
        errors.append("stop_loss_pct must be in (0,1)")
    if cfg.take_profit_pct <= 0.0 or cfg.take_profit_pct > 2.0:
        errors.append("take_profit_pct must be in (0,2]")
    if cfg.take_profit_pct <= cfg.stop_loss_pct:
        errors.append("take_profit_pct must be greater than stop_loss_pct")
    if cfg.enable_trailing_stop:
        if cfg.trailing_stop_pct is None:
            errors.append("trailing_stop_pct is required when enable_trailing_stop=true")
        elif cfg.trailing_stop_pct <= 0.0 or cfg.trailing_stop_pct >= 1.0:
            errors.append("trailing_stop_pct must be in (0,1)")
    else:
        if cfg.trailing_stop_pct is not None:
            errors.append("trailing_stop_pct must be null when enable_trailing_stop=false")
    if cfg.allow_hold_extension:
        if cfg.max_hold_extension_minutes < 1 or cfg.max_hold_extension_minutes > 30:
            errors.append("max_hold_extension_minutes must be in [1,30] when allow_hold_extension=true")
        if cfg.extension_min_model_prob is None:
            errors.append("extension_min_model_prob is required when allow_hold_extension=true")
        elif cfg.extension_min_model_prob < 0.0 or cfg.extension_min_model_prob > 1.0:
            errors.append("extension_min_model_prob must be in [0,1]")
    else:
        if cfg.max_hold_extension_minutes != 0:
            errors.append("max_hold_extension_minutes must be 0 when allow_hold_extension=false")
        if cfg.extension_min_model_prob is not None:
            errors.append("extension_min_model_prob must be null when allow_hold_extension=false")
    if cfg.move_to_break_even_at_profit_pct is not None:
        if cfg.move_to_break_even_at_profit_pct < 0.0 or cfg.move_to_break_even_at_profit_pct > 2.0:
            errors.append("move_to_break_even_at_profit_pct must be in [0,2]")
    if cfg.intrabar_tie_break not in {"sl", "tp"}:
        errors.append("intrabar_tie_break must be one of: sl,tp")
    return errors


def parse_policy(payload: Dict[str, object]) -> DynamicExitPolicyConfig:
    default = DynamicExitPolicyConfig()
    merged = {**asdict(default), **payload}
    cfg = DynamicExitPolicyConfig(
        stop_loss_pct=float(merged["stop_loss_pct"]),
        take_profit_pct=float(merged["take_profit_pct"]),
        enable_trailing_stop=bool(merged["enable_trailing_stop"]),
        trailing_stop_pct=None if merged["trailing_stop_pct"] is None else float(merged["trailing_stop_pct"]),
        move_to_break_even_at_profit_pct=(
            None
            if merged["move_to_break_even_at_profit_pct"] is None
            else float(merged["move_to_break_even_at_profit_pct"])
        ),
        allow_hold_extension=bool(merged["allow_hold_extension"]),
        max_hold_extension_minutes=int(merged["max_hold_extension_minutes"]),
        extension_min_model_prob=(
            None if merged["extension_min_model_prob"] is None else float(merged["extension_min_model_prob"])
        ),
        intrabar_tie_break=str(merged["intrabar_tie_break"]).lower(),
    )
    errors = validate_policy_config(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    return cfg


def _normalize_bars(bars: Sequence[Dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(bars))
    for col in ("high", "low", "close"):
        if col not in frame.columns:
            frame[col] = np.nan
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["high", "low", "close"]).reset_index(drop=True)
    return frame


def _resolve_reason_for_stop(trailing_active: bool, stop_price: float, hard_stop: float) -> str:
    if trailing_active and np.isfinite(stop_price) and stop_price > (hard_stop + 1e-12):
        return "trail"
    return "sl"


def _hit_decision(
    bar_high: float,
    bar_low: float,
    tp_price: float,
    stop_price: float,
    intrabar_tie_break: str,
    trailing_active: bool,
    hard_stop: float,
) -> Optional[str]:
    hit_tp = bool(np.isfinite(bar_high) and bar_high >= tp_price)
    hit_sl = bool(np.isfinite(bar_low) and bar_low <= stop_price)
    if hit_tp and hit_sl:
        if intrabar_tie_break == "tp":
            return "tp"
        return _resolve_reason_for_stop(trailing_active=trailing_active, stop_price=stop_price, hard_stop=hard_stop)
    if hit_tp:
        return "tp"
    if hit_sl:
        return _resolve_reason_for_stop(trailing_active=trailing_active, stop_price=stop_price, hard_stop=hard_stop)
    return None


def simulate_dynamic_exit(
    entry_price: float,
    bars: Sequence[Dict[str, object]],
    horizon_minutes: int,
    model_prob: float,
    config: DynamicExitPolicyConfig,
) -> Dict[str, object]:
    if not np.isfinite(entry_price) or entry_price <= 0.0:
        return {
            "ok": False,
            "error": "invalid_entry_price",
            "exit_reason": "invalid",
        }
    if horizon_minutes < 1:
        return {
            "ok": False,
            "error": "invalid_horizon",
            "exit_reason": "invalid",
        }
    errors = validate_policy_config(config)
    if errors:
        return {
            "ok": False,
            "error": "; ".join(errors),
            "exit_reason": "invalid",
        }

    frame = _normalize_bars(bars)
    if frame.empty:
        return {
            "ok": False,
            "error": "empty_bars",
            "exit_reason": "invalid",
        }

    hard_stop = float(entry_price * (1.0 - config.stop_loss_pct))
    stop_price = hard_stop
    tp_price = float(entry_price * (1.0 + config.take_profit_pct))
    highest = float(entry_price)
    trailing_active = False
    break_even_active = False
    hold_extended = False

    def _bar_iter(max_bars: int):
        n = min(max_bars, len(frame))
        for i in range(n):
            row = frame.iloc[i]
            yield i, float(row["high"]), float(row["low"]), float(row["close"])

    # Phase 1: baseline horizon
    for i, high, low, close in _bar_iter(horizon_minutes):
        highest = max(highest, high)
        if config.move_to_break_even_at_profit_pct is not None:
            be_trigger = entry_price * (1.0 + config.move_to_break_even_at_profit_pct)
            if high >= be_trigger:
                break_even_active = True
                stop_price = max(stop_price, entry_price)

        if config.enable_trailing_stop:
            activation_gain = config.move_to_break_even_at_profit_pct if config.move_to_break_even_at_profit_pct is not None else 0.0
            trailing_trigger = entry_price * (1.0 + activation_gain)
            if high >= trailing_trigger:
                trailing_active = True
            if trailing_active and config.trailing_stop_pct is not None:
                trailing_stop = highest * (1.0 - config.trailing_stop_pct)
                stop_price = max(stop_price, trailing_stop)

        decision = _hit_decision(
            bar_high=high,
            bar_low=low,
            tp_price=tp_price,
            stop_price=stop_price,
            intrabar_tie_break=config.intrabar_tie_break,
            trailing_active=trailing_active,
            hard_stop=hard_stop,
        )
        if decision is not None:
            exit_price = tp_price if decision == "tp" else stop_price
            return {
                "ok": True,
                "exit_reason": decision,
                "exit_offset_min": int(i),
                "exit_price": float(exit_price),
                "realized_return": float((exit_price - entry_price) / entry_price),
                "stop_price_final": float(stop_price),
                "tp_price": float(tp_price),
                "trailing_active": bool(trailing_active),
                "break_even_active": bool(break_even_active),
                "hold_extended": bool(hold_extended),
            }

    # Hold extension (if eligible and bars are available)
    extension_eligible = bool(
        config.allow_hold_extension
        and config.max_hold_extension_minutes > 0
        and config.extension_min_model_prob is not None
        and float(model_prob) >= float(config.extension_min_model_prob)
    )
    max_bars = horizon_minutes
    if extension_eligible:
        max_bars = horizon_minutes + int(config.max_hold_extension_minutes)
        hold_extended = True
        for i, high, low, close in _bar_iter(max_bars):
            if i < horizon_minutes:
                continue
            highest = max(highest, high)
            if config.enable_trailing_stop and trailing_active and config.trailing_stop_pct is not None:
                stop_price = max(stop_price, highest * (1.0 - config.trailing_stop_pct))
            decision = _hit_decision(
                bar_high=high,
                bar_low=low,
                tp_price=tp_price,
                stop_price=stop_price,
                intrabar_tie_break=config.intrabar_tie_break,
                trailing_active=trailing_active,
                hard_stop=hard_stop,
            )
            if decision is not None:
                exit_price = tp_price if decision == "tp" else stop_price
                return {
                    "ok": True,
                    "exit_reason": decision,
                    "exit_offset_min": int(i),
                    "exit_price": float(exit_price),
                    "realized_return": float((exit_price - entry_price) / entry_price),
                    "stop_price_final": float(stop_price),
                    "tp_price": float(tp_price),
                    "trailing_active": bool(trailing_active),
                    "break_even_active": bool(break_even_active),
                    "hold_extended": bool(hold_extended),
                }

    final_idx = min(max_bars, len(frame)) - 1
    if final_idx < 0:
        return {
            "ok": False,
            "error": "insufficient_bars",
            "exit_reason": "invalid",
        }
    final_close = float(frame.iloc[final_idx]["close"])
    return {
        "ok": True,
        "exit_reason": "time",
        "exit_offset_min": int(final_idx),
        "exit_price": final_close,
        "realized_return": float((final_close - entry_price) / entry_price),
        "stop_price_final": float(stop_price),
        "tp_price": float(tp_price),
        "trailing_active": bool(trailing_active),
        "break_even_active": bool(break_even_active),
        "hold_extended": bool(hold_extended),
    }


def _default_scenarios() -> List[Dict[str, object]]:
    return [
        {
            "name": "tp_first",
            "entry_price": 100.0,
            "horizon_minutes": 3,
            "model_prob": 0.60,
            "bars": [
                {"high": 103.0, "low": 99.0, "close": 101.0},
                {"high": 126.0, "low": 100.0, "close": 123.0},
                {"high": 127.0, "low": 120.0, "close": 126.0},
            ],
        },
        {
            "name": "sl_first",
            "entry_price": 100.0,
            "horizon_minutes": 3,
            "model_prob": 0.60,
            "bars": [
                {"high": 101.0, "low": 98.0, "close": 99.0},
                {"high": 100.0, "low": 87.0, "close": 89.0},
                {"high": 95.0, "low": 86.0, "close": 88.0},
            ],
        },
        {
            "name": "extension_time_exit",
            "entry_price": 100.0,
            "horizon_minutes": 3,
            "model_prob": 0.80,
            "bars": [
                {"high": 104.0, "low": 98.0, "close": 102.0},
                {"high": 106.0, "low": 100.0, "close": 104.0},
                {"high": 107.0, "low": 101.0, "close": 105.0},
                {"high": 108.0, "low": 102.0, "close": 106.0},
                {"high": 109.0, "low": 103.0, "close": 107.0},
            ],
        },
    ]


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dynamic exit policy simulator")
    parser.add_argument("--policy-json", default=None, help="Policy config JSON path")
    parser.add_argument("--scenarios-json", default=None, help="Scenario array JSON path")
    parser.add_argument("--out", default="ml_pipeline/artifacts/t17_dynamic_exit_policy_report.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.policy_json:
        policy_path = Path(args.policy_json)
        if not policy_path.exists():
            print(f"ERROR: policy file not found: {policy_path}")
            return 2
        policy_payload = json.loads(policy_path.read_text(encoding="utf-8"))
        if not isinstance(policy_payload, dict):
            print("ERROR: policy JSON must be object")
            return 2
        config = parse_policy(policy_payload)
    else:
        config = DynamicExitPolicyConfig(
            stop_loss_pct=0.12,
            take_profit_pct=0.24,
            enable_trailing_stop=True,
            trailing_stop_pct=0.08,
            move_to_break_even_at_profit_pct=0.06,
            allow_hold_extension=True,
            max_hold_extension_minutes=2,
            extension_min_model_prob=0.70,
            intrabar_tie_break="sl",
        )

    if args.scenarios_json:
        path = Path(args.scenarios_json)
        if not path.exists():
            print(f"ERROR: scenarios file not found: {path}")
            return 2
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            print("ERROR: scenarios JSON must be array")
            return 2
        scenarios = payload
    else:
        scenarios = _default_scenarios()

    rows: List[Dict[str, object]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        result = simulate_dynamic_exit(
            entry_price=float(scenario.get("entry_price", 100.0)),
            bars=scenario.get("bars", []),
            horizon_minutes=int(scenario.get("horizon_minutes", 3)),
            model_prob=float(scenario.get("model_prob", 0.5)),
            config=config,
        )
        rows.append({"name": str(scenario.get("name", "scenario")), "result": result})

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": asdict(config),
        "scenario_count": int(len(rows)),
        "scenarios": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Scenarios: {len(rows)}")
    print(f"Output: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
