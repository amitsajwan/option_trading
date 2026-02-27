import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from .fill_model import FillModelConfig, config_to_dict as fill_config_to_dict, estimate_slippage_return


@dataclass(frozen=True)
class ExecutionSimulatorConfig:
    order_latency_ms: int = 350
    exchange_latency_ms: int = 250
    max_participation_rate: float = 0.20
    fallback_volume: float = 100.0
    fee_per_fill_return: float = 0.0003
    default_order_qty: float = 1.0
    min_fill_qty: float = 0.0
    force_liquidate_end: bool = True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def _safe_side(value: object) -> str:
    side = str(value or "").upper()
    return side if side in {"CE", "PE"} else ""


def _load_jsonl(path: Path) -> pd.DataFrame:
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
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df.get("timestamp"), errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


class ParquetSnapshotSource:
    def __init__(self, labeled_df: pd.DataFrame):
        frame = labeled_df.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        frame["ce_next_close"] = frame["opt_0_ce_close"].shift(-1)
        frame["pe_next_close"] = frame["opt_0_pe_close"].shift(-1)
        self.df = frame
        self.index = pd.DatetimeIndex(frame["timestamp"])

    def get_snapshot(self, ts: pd.Timestamp) -> Optional[Dict[str, object]]:
        if len(self.df) == 0:
            return None
        loc = self.index.searchsorted(ts, side="right") - 1
        if loc < 0:
            return None
        row = self.df.iloc[int(loc)]
        return {
            "snapshot_timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
            "opt_0_ce_close": _safe_float(row.get("opt_0_ce_close")),
            "opt_0_ce_high": _safe_float(row.get("opt_0_ce_high")),
            "opt_0_ce_low": _safe_float(row.get("opt_0_ce_low")),
            "opt_0_ce_volume": _safe_float(row.get("opt_0_ce_volume")),
            "opt_0_pe_close": _safe_float(row.get("opt_0_pe_close")),
            "opt_0_pe_high": _safe_float(row.get("opt_0_pe_high")),
            "opt_0_pe_low": _safe_float(row.get("opt_0_pe_low")),
            "opt_0_pe_volume": _safe_float(row.get("opt_0_pe_volume")),
            "ce_next_close": _safe_float(row.get("ce_next_close")),
            "pe_next_close": _safe_float(row.get("pe_next_close")),
            "bid_qty_total": _safe_float(row.get("depth_total_bid_qty")),
            "ask_qty_total": _safe_float(row.get("depth_total_ask_qty")),
        }


class ApiSnapshotSource:
    def __init__(
        self,
        *,
        instrument: str,
        market_api_base: str = "http://127.0.0.1:8004",
        dashboard_api_base: str = "http://127.0.0.1:8002",
        timeout_seconds: float = 5.0,
    ):
        self.instrument = str(instrument)
        self.market_api_base = str(market_api_base).rstrip("/")
        self.dashboard_api_base = str(dashboard_api_base).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def _get_json(self, url: str) -> object:
        resp = requests.get(url, timeout=self.timeout_seconds)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _extract_list(payload: object, keys: Tuple[str, ...]) -> List[dict]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in keys:
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    @staticmethod
    def _depth_total(levels: object) -> float:
        if not isinstance(levels, list):
            return float("nan")
        qty = 0.0
        for level in levels[:5]:
            if isinstance(level, dict):
                qty += max(0.0, _safe_float(level.get("quantity")))
        return float(qty)

    @staticmethod
    def _resolve_atm_strike(chain: Dict[str, object]) -> Optional[Dict[str, object]]:
        strikes = chain.get("strikes")
        if not isinstance(strikes, list) or len(strikes) == 0:
            return None
        rows = [x for x in strikes if isinstance(x, dict)]
        if not rows:
            return None
        fut = _safe_float(chain.get("futures_price"))
        if not np.isfinite(fut):
            fut = _safe_float(chain.get("underlying_price"))
        if not np.isfinite(fut):
            return rows[0]

        def strike_value(item: Dict[str, object]) -> float:
            return _safe_float(item.get("strike"))

        return min(rows, key=lambda x: abs(strike_value(x) - fut))

    def get_snapshot(self, _ts: pd.Timestamp) -> Optional[Dict[str, object]]:
        try:
            options_raw = self._get_json(f"{self.dashboard_api_base}/api/market-data/options/{self.instrument}")
            depth_raw = self._get_json(f"{self.dashboard_api_base}/api/market-data/depth/{self.instrument}")
        except Exception:
            return None

        chain = options_raw if isinstance(options_raw, dict) else {}
        depth = depth_raw if isinstance(depth_raw, dict) else {}
        atm = self._resolve_atm_strike(chain)
        if not atm:
            return None

        ce_close = _safe_float(atm.get("ce_ltp"))
        pe_close = _safe_float(atm.get("pe_ltp"))
        ce_vol = _safe_float(atm.get("ce_volume"))
        pe_vol = _safe_float(atm.get("pe_volume"))
        if not np.isfinite(ce_vol):
            ce_vol = _safe_float((atm.get("CE") or {}).get("volume")) if isinstance(atm.get("CE"), dict) else float("nan")
        if not np.isfinite(pe_vol):
            pe_vol = _safe_float((atm.get("PE") or {}).get("volume")) if isinstance(atm.get("PE"), dict) else float("nan")

        ts = str(chain.get("timestamp") or depth.get("timestamp") or _utc_now())
        bid_qty_total = self._depth_total(depth.get("buy"))
        ask_qty_total = self._depth_total(depth.get("sell"))
        return {
            "snapshot_timestamp": ts,
            "opt_0_ce_close": ce_close,
            "opt_0_ce_high": ce_close,
            "opt_0_ce_low": ce_close,
            "opt_0_ce_volume": ce_vol,
            "opt_0_pe_close": pe_close,
            "opt_0_pe_high": pe_close,
            "opt_0_pe_low": pe_close,
            "opt_0_pe_volume": pe_vol,
            "ce_next_close": ce_close,
            "pe_next_close": pe_close,
            "bid_qty_total": bid_qty_total,
            "ask_qty_total": ask_qty_total,
        }


def _event_side(row: pd.Series) -> str:
    action = str(row.get("action", "")).upper()
    if action == "BUY_CE":
        return "CE"
    if action == "BUY_PE":
        return "PE"
    position = row.get("position")
    if isinstance(position, dict):
        return _safe_side(position.get("side"))
    return ""


def _estimate_latency_impact_return(snapshot: Dict[str, object], side: str, action_kind: str, latency_ms: int) -> float:
    prefix = "ce" if side == "CE" else "pe"
    now_close = _safe_float(snapshot.get(f"opt_0_{prefix}_close"))
    next_close = _safe_float(snapshot.get(f"{prefix}_next_close"))
    if (not np.isfinite(now_close)) or now_close <= 0 or (not np.isfinite(next_close)):
        return 0.0
    one_bar_return = float((next_close - now_close) / now_close)
    latency_fraction = float(max(0.0, latency_ms) / 60000.0)
    scaled = float(one_bar_return * latency_fraction)
    if action_kind == "BUY":
        return float(max(0.0, scaled))
    return float(max(0.0, -scaled))


def _simulate_fill(
    *,
    snapshot: Dict[str, object],
    side: str,
    action_kind: str,
    requested_qty: float,
    sim_cfg: ExecutionSimulatorConfig,
    fill_cfg: FillModelConfig,
) -> Dict[str, object]:
    prefix = "ce" if side == "CE" else "pe"
    close = _safe_float(snapshot.get(f"opt_0_{prefix}_close"))
    if (not np.isfinite(close)) or close <= 0:
        return {
            "status": "rejected",
            "reason": "invalid_market_price",
            "requested_qty": float(requested_qty),
            "filled_qty": 0.0,
            "fill_ratio": 0.0,
            "fill_price": float("nan"),
            "model_slippage_return": float("nan"),
            "latency_impact_return": float("nan"),
            "fee_return": 0.0,
        }

    available_by_volume = _safe_float(snapshot.get(f"opt_0_{prefix}_volume"))
    if (not np.isfinite(available_by_volume)) or available_by_volume <= 0:
        available_by_volume = float(sim_cfg.fallback_volume)
    available_qty = float(max(0.0, available_by_volume * float(sim_cfg.max_participation_rate)))

    book_side_qty = _safe_float(snapshot.get("ask_qty_total" if action_kind == "BUY" else "bid_qty_total"))
    if np.isfinite(book_side_qty) and book_side_qty >= 0:
        available_qty = float(min(available_qty, book_side_qty))

    requested_qty = float(max(0.0, requested_qty))
    filled_qty = float(min(requested_qty, available_qty))
    if filled_qty < float(sim_cfg.min_fill_qty):
        filled_qty = 0.0
    fill_ratio = float(filled_qty / requested_qty) if requested_qty > 0 else 0.0
    if filled_qty <= 0:
        return {
            "status": "rejected",
            "reason": "no_liquidity",
            "requested_qty": float(requested_qty),
            "filled_qty": 0.0,
            "fill_ratio": 0.0,
            "fill_price": float("nan"),
            "model_slippage_return": 0.0,
            "latency_impact_return": 0.0,
            "fee_return": 0.0,
        }

    model_slippage = float(estimate_slippage_return(pd.Series(snapshot), side=side, config=fill_cfg))
    latency_ms = int(sim_cfg.order_latency_ms + sim_cfg.exchange_latency_ms)
    latency_impact = float(_estimate_latency_impact_return(snapshot, side=side, action_kind=action_kind, latency_ms=latency_ms))
    slippage_total = float(model_slippage + latency_impact)
    if action_kind == "BUY":
        fill_price = float(close * (1.0 + slippage_total))
    else:
        fill_price = float(close * max(0.0, 1.0 - slippage_total))
    status = "filled" if np.isclose(fill_ratio, 1.0) else "partial_fill"
    return {
        "status": status,
        "reason": "ok",
        "requested_qty": float(requested_qty),
        "filled_qty": float(filled_qty),
        "fill_ratio": float(fill_ratio),
        "fill_price": float(fill_price),
        "model_slippage_return": float(model_slippage),
        "latency_impact_return": float(latency_impact),
        "fee_return": float(sim_cfg.fee_per_fill_return),
    }


def run_execution_simulation(
    *,
    events_df: pd.DataFrame,
    snapshot_source: object,
    sim_cfg: ExecutionSimulatorConfig,
    fill_cfg: FillModelConfig,
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    if len(events_df) == 0:
        empty = pd.DataFrame(columns=["timestamp", "event_type", "sim_status"])
        return empty, {
            "created_at_utc": _utc_now(),
            "status": "no_data",
            "events_total": 0,
            "execution_events": 0,
        }

    events = events_df.copy()
    events["timestamp"] = pd.to_datetime(events["timestamp"], errors="coerce")
    events = events.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    exec_rows: List[Dict[str, object]] = []
    trades: List[Dict[str, object]] = []
    open_pos: Optional[Dict[str, object]] = None

    entry_intents = 0
    exit_intents = 0
    skipped_events = 0
    snapshot_misses = 0
    rejects = 0

    for row in events.to_dict(orient="records"):
        event_type = str(row.get("event_type", "")).upper()
        ts = pd.Timestamp(row["timestamp"])
        side = _event_side(pd.Series(row))
        if event_type not in {"ENTRY", "EXIT"}:
            skipped_events += 1
            continue
        if side not in {"CE", "PE"}:
            skipped_events += 1
            continue

        snapshot = snapshot_source.get_snapshot(ts)
        if snapshot is None:
            snapshot_misses += 1
            continue

        action_kind = "BUY" if event_type == "ENTRY" else "SELL"
        if event_type == "ENTRY":
            entry_intents += 1
            if open_pos is not None:
                rejects += 1
                exec_rows.append(
                    {
                        "timestamp": ts,
                        "event_type": event_type,
                        "side": side,
                        "action_kind": action_kind,
                        "sim_status": "rejected",
                        "reason": "position_already_open",
                        "requested_qty": float(sim_cfg.default_order_qty),
                        "filled_qty": 0.0,
                        "fill_ratio": 0.0,
                    }
                )
                continue
            fill = _simulate_fill(
                snapshot=snapshot,
                side=side,
                action_kind=action_kind,
                requested_qty=float(sim_cfg.default_order_qty),
                sim_cfg=sim_cfg,
                fill_cfg=fill_cfg,
            )
            if str(fill["status"]) == "rejected":
                rejects += 1
            else:
                open_pos = {
                    "side": side,
                    "qty": float(fill["filled_qty"]),
                    "entry_price": float(fill["fill_price"]),
                    "entry_ts": ts,
                    "entry_fee_return": float(fill["fee_return"]),
                }
            exec_rows.append(
                {
                    "timestamp": ts,
                    "event_type": event_type,
                    "side": side,
                    "action_kind": action_kind,
                    "snapshot_timestamp": snapshot.get("snapshot_timestamp"),
                    **fill,
                }
            )
            continue

        # EXIT
        exit_intents += 1
        if open_pos is None:
            rejects += 1
            exec_rows.append(
                {
                    "timestamp": ts,
                    "event_type": event_type,
                    "side": side,
                    "action_kind": action_kind,
                    "sim_status": "rejected",
                    "reason": "no_open_position",
                    "requested_qty": 0.0,
                    "filled_qty": 0.0,
                    "fill_ratio": 0.0,
                }
            )
            continue

        fill = _simulate_fill(
            snapshot=snapshot,
            side=str(open_pos["side"]),
            action_kind=action_kind,
            requested_qty=float(open_pos["qty"]),
            sim_cfg=sim_cfg,
            fill_cfg=fill_cfg,
        )
        if str(fill["status"]) == "rejected":
            rejects += 1
        else:
            exit_qty = float(fill["filled_qty"])
            remaining = float(open_pos["qty"] - exit_qty)
            entry_price = float(open_pos["entry_price"])
            exit_price = float(fill["fill_price"])
            if exit_qty > 0 and entry_price > 0:
                gross = float((exit_price - entry_price) / entry_price)
                net = float(gross - float(open_pos["entry_fee_return"]) - float(fill["fee_return"]))
                trades.append(
                    {
                        "entry_timestamp": pd.Timestamp(open_pos["entry_ts"]),
                        "exit_timestamp": ts,
                        "side": str(open_pos["side"]),
                        "entry_price": float(entry_price),
                        "exit_price": float(exit_price),
                        "qty_closed": float(exit_qty),
                        "gross_return": float(gross),
                        "net_return": float(net),
                        "exit_reason": str(row.get("event_reason", "")),
                    }
                )
            if remaining <= 0:
                open_pos = None
            else:
                open_pos["qty"] = remaining

        exec_rows.append(
            {
                "timestamp": ts,
                "event_type": event_type,
                "side": _safe_side(open_pos["side"]) if open_pos is not None else side,
                "action_kind": action_kind,
                "snapshot_timestamp": snapshot.get("snapshot_timestamp"),
                **fill,
            }
        )

    if (open_pos is not None) and bool(sim_cfg.force_liquidate_end) and len(events) > 0:
        last_ts = pd.Timestamp(events.iloc[-1]["timestamp"])
        snapshot = snapshot_source.get_snapshot(last_ts)
        if snapshot is not None:
            fill = _simulate_fill(
                snapshot=snapshot,
                side=str(open_pos["side"]),
                action_kind="SELL",
                requested_qty=float(open_pos["qty"]),
                sim_cfg=sim_cfg,
                fill_cfg=fill_cfg,
            )
            exec_rows.append(
                {
                    "timestamp": last_ts,
                    "event_type": "FORCED_EXIT",
                    "side": str(open_pos["side"]),
                    "action_kind": "SELL",
                    "snapshot_timestamp": snapshot.get("snapshot_timestamp"),
                    **fill,
                }
            )
            if str(fill["status"]) != "rejected" and float(fill["filled_qty"]) > 0:
                entry_price = float(open_pos["entry_price"])
                exit_price = float(fill["fill_price"])
                gross = float((exit_price - entry_price) / entry_price) if entry_price > 0 else float("nan")
                net = float(gross - float(open_pos["entry_fee_return"]) - float(fill["fee_return"]))
                trades.append(
                    {
                        "entry_timestamp": pd.Timestamp(open_pos["entry_ts"]),
                        "exit_timestamp": last_ts,
                        "side": str(open_pos["side"]),
                        "entry_price": float(entry_price),
                        "exit_price": float(exit_price),
                        "qty_closed": float(fill["filled_qty"]),
                        "gross_return": float(gross),
                        "net_return": float(net),
                        "exit_reason": "forced_liquidation",
                    }
                )
                remaining = float(open_pos["qty"] - float(fill["filled_qty"]))
                open_pos = None if remaining <= 0 else {"side": open_pos["side"], "qty": remaining}

    exec_df = pd.DataFrame(exec_rows)
    trades_df = pd.DataFrame(trades)
    closed = int(len(trades_df))
    partials = int((exec_df.get("status", pd.Series(dtype=str)) == "partial_fill").sum()) if len(exec_df) else 0
    fills = int((exec_df.get("status", pd.Series(dtype=str)).isin(["filled", "partial_fill"])).sum()) if len(exec_df) else 0
    mean_fill_ratio = float(exec_df["fill_ratio"].mean()) if len(exec_df) and "fill_ratio" in exec_df.columns else 0.0
    net_sum = float(trades_df["net_return"].sum()) if len(trades_df) else 0.0
    win_rate = float((trades_df["net_return"] > 0).mean()) if len(trades_df) else 0.0
    report = {
        "created_at_utc": _utc_now(),
        "status": "ok",
        "events_total": int(len(events)),
        "entry_intents": int(entry_intents),
        "exit_intents": int(exit_intents),
        "skipped_events": int(skipped_events),
        "snapshot_misses": int(snapshot_misses),
        "execution_events": int(len(exec_df)),
        "fills_total": int(fills),
        "rejects_total": int(rejects),
        "partial_fills_total": int(partials),
        "mean_fill_ratio": float(mean_fill_ratio),
        "closed_trades": int(closed),
        "net_return_sum": float(net_sum),
        "mean_net_return_per_trade": float(trades_df["net_return"].mean()) if len(trades_df) else 0.0,
        "win_rate": float(win_rate),
        "open_position_end_qty": float(open_pos["qty"]) if isinstance(open_pos, dict) and "qty" in open_pos else 0.0,
        "simulator_config": asdict(sim_cfg),
        "fill_model": fill_config_to_dict(fill_cfg),
    }
    return exec_df, report


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Execution simulator V2 (latency + partial fill)")
    parser.add_argument("--events-jsonl", default="ml_pipeline/artifacts/t22_exit_aware_paper_events.jsonl")
    parser.add_argument("--market-source", choices=["parquet", "api"], default="parquet")
    parser.add_argument("--labeled-data", default="ml_pipeline/artifacts/t05_labeled_features.parquet")
    parser.add_argument("--instrument", default="BANKNIFTY-I")
    parser.add_argument("--market-api-base", default="http://127.0.0.1:8004")
    parser.add_argument("--dashboard-api-base", default="http://127.0.0.1:8002")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--order-latency-ms", type=int, default=350)
    parser.add_argument("--exchange-latency-ms", type=int, default=250)
    parser.add_argument("--max-participation-rate", type=float, default=0.20)
    parser.add_argument("--fallback-volume", type=float, default=100.0)
    parser.add_argument("--fee-per-fill-return", type=float, default=0.0003)
    parser.add_argument("--default-order-qty", type=float, default=1.0)
    parser.add_argument("--min-fill-qty", type=float, default=0.0)
    parser.add_argument("--force-liquidate-end", action="store_true")
    parser.add_argument("--fill-model", default="spread_fraction", choices=["constant", "spread_fraction", "liquidity_adjusted"])
    parser.add_argument("--fill-constant", type=float, default=0.0)
    parser.add_argument("--fill-spread-fraction", type=float, default=0.5)
    parser.add_argument("--fill-volume-impact", type=float, default=0.02)
    parser.add_argument("--fill-min", type=float, default=0.0)
    parser.add_argument("--fill-max", type=float, default=0.01)
    parser.add_argument("--events-out", default="ml_pipeline/artifacts/t30_execution_events.parquet")
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t30_execution_report.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    events_path = Path(args.events_jsonl)
    if not events_path.exists():
        print(f"ERROR: events file not found: {events_path}")
        return 2
    events_df = _load_jsonl(events_path)
    if len(events_df) == 0:
        print("ERROR: no valid events in input jsonl")
        return 2

    if args.market_source == "parquet":
        labeled_path = Path(args.labeled_data)
        if not labeled_path.exists():
            print(f"ERROR: labeled data not found: {labeled_path}")
            return 2
        snap_source = ParquetSnapshotSource(pd.read_parquet(labeled_path))
    else:
        snap_source = ApiSnapshotSource(
            instrument=str(args.instrument),
            market_api_base=str(args.market_api_base),
            dashboard_api_base=str(args.dashboard_api_base),
            timeout_seconds=float(args.timeout_seconds),
        )

    sim_cfg = ExecutionSimulatorConfig(
        order_latency_ms=int(args.order_latency_ms),
        exchange_latency_ms=int(args.exchange_latency_ms),
        max_participation_rate=float(args.max_participation_rate),
        fallback_volume=float(args.fallback_volume),
        fee_per_fill_return=float(args.fee_per_fill_return),
        default_order_qty=float(args.default_order_qty),
        min_fill_qty=float(args.min_fill_qty),
        force_liquidate_end=bool(args.force_liquidate_end),
    )
    fill_cfg = FillModelConfig(
        model=str(args.fill_model),
        constant_slippage=float(args.fill_constant),
        spread_fraction=float(args.fill_spread_fraction),
        volume_impact_coeff=float(args.fill_volume_impact),
        min_slippage=float(args.fill_min),
        max_slippage=float(args.fill_max),
    )
    exec_df, report = run_execution_simulation(
        events_df=events_df,
        snapshot_source=snap_source,
        sim_cfg=sim_cfg,
        fill_cfg=fill_cfg,
    )
    report["market_source"] = str(args.market_source)
    report["events_input"] = str(events_path)

    events_out = Path(args.events_out)
    report_out = Path(args.report_out)
    events_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.parent.mkdir(parents=True, exist_ok=True)
    exec_df.to_parquet(events_out, index=False)
    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Execution events: {len(exec_df)}")
    print(f"Closed trades: {report['closed_trades']}")
    print(f"Net return sum: {report['net_return_sum']}")
    print(f"Events output: {events_out}")
    print(f"Report output: {report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
