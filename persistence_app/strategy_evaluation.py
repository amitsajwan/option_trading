from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
from datetime import datetime
from statistics import median
from typing import Any, Iterable, Optional

try:
    from pymongo import MongoClient
except Exception:  # pragma: no cover
    MongoClient = None


_REASON_RE = re.compile(r"^\[(?P<regime>[^\]]+)\]\s+(?P<strategy>[^:]+):")


def _mongo_client() -> MongoClient:
    if MongoClient is None:
        raise RuntimeError("pymongo_not_installed")
    uri = str(os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    if uri:
        return MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000)
    return MongoClient(
        host=str(os.getenv("MONGO_HOST") or "localhost"),
        port=int(os.getenv("MONGO_PORT") or "27017"),
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=5000,
    )


def _date_filter(*, date_from: Optional[str], date_to: Optional[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if date_from:
        values["$gte"] = str(date_from)
    if date_to:
        values["$lte"] = str(date_to)
    return {"trade_date_ist": values} if values else {}


def _parse_reason(reason: str) -> tuple[Optional[str], Optional[str]]:
    match = _REASON_RE.match(str(reason or "").strip())
    if not match:
        return None, None
    regime = str(match.group("regime") or "").strip() or None
    strategy = str(match.group("strategy") or "").strip() or None
    return strategy, regime


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _iso_or_none(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return isoformat_ist(value, naive_mode=TimestampSourceMode.LEGACY_MONGO_UTC)
    text = str(value or "").strip()
    return text or None


def _load_signal_map(signal_coll: Any, *, date_match: dict[str, Any]) -> dict[str, dict[str, Any]]:
    projection = {
        "_id": 0,
        "signal_id": 1,
        "regime": 1,
        "confidence": 1,
        "reason": 1,
        "payload.signal": 1,
        "trade_date_ist": 1,
    }
    output: dict[str, dict[str, Any]] = {}
    for doc in signal_coll.find(date_match, projection):
        signal_id = str(doc.get("signal_id") or "").strip()
        if not signal_id:
            continue
        payload_signal = (((doc.get("payload") or {}).get("signal")) if isinstance(doc.get("payload"), dict) else {}) or {}
        if not isinstance(payload_signal, dict):
            payload_signal = {}
        output[signal_id] = {
            "signal_id": signal_id,
            "regime": str(doc.get("regime") or payload_signal.get("regime") or "").strip() or None,
            "confidence": _safe_float(doc.get("confidence") if doc.get("confidence") is not None else payload_signal.get("confidence")),
            "reason": str(doc.get("reason") or payload_signal.get("reason") or "").strip(),
            "contributing_strategies": list(payload_signal.get("contributing_strategies") or []),
            "timestamp": _iso_or_none(payload_signal.get("timestamp") or doc.get("timestamp")),
            "trade_date_ist": str(doc.get("trade_date_ist") or "").strip() or None,
        }
    return output


def _load_positions(position_coll: Any, *, date_match: dict[str, Any]) -> dict[str, dict[str, Any]]:
    projection = {
        "_id": 0,
        "position_id": 1,
        "event": 1,
        "timestamp": 1,
        "trade_date_ist": 1,
        "payload.position": 1,
    }
    positions: dict[str, dict[str, Any]] = {}
    for doc in position_coll.find(date_match, projection).sort("timestamp", 1):
        position_id = str(doc.get("position_id") or "").strip()
        if not position_id:
            continue
        payload_position = (((doc.get("payload") or {}).get("position")) if isinstance(doc.get("payload"), dict) else {}) or {}
        if not isinstance(payload_position, dict):
            payload_position = {}
        entry = positions.setdefault(position_id, {"position_id": position_id})
        event = str(doc.get("event") or payload_position.get("event") or "").strip().upper()
        if event == "POSITION_OPEN":
            entry["open"] = payload_position
            entry["open_doc"] = doc
        elif event == "POSITION_CLOSE":
            entry["close"] = payload_position
            entry["close_doc"] = doc
    return positions


def _primary_strategy(*, signal_doc: dict[str, Any], open_position: dict[str, Any]) -> Optional[str]:
    reason_strategy, _ = _parse_reason(str(signal_doc.get("reason") or open_position.get("reason") or ""))
    if reason_strategy:
        return reason_strategy
    strategies = signal_doc.get("contributing_strategies")
    if isinstance(strategies, list) and strategies:
        first = str(strategies[0] or "").strip()
        return first or None
    return None


def _trade_from_docs(position_id: str, docs: dict[str, Any], signal_map: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    open_position = docs.get("open")
    close_position = docs.get("close")
    if not isinstance(open_position, dict) or not isinstance(close_position, dict):
        return None

    signal_id = str(open_position.get("signal_id") or "").strip()
    signal_doc = signal_map.get(signal_id, {})
    strategy = _primary_strategy(signal_doc=signal_doc, open_position=open_position)
    _, regime_from_reason = _parse_reason(str(signal_doc.get("reason") or open_position.get("reason") or ""))
    regime = str(signal_doc.get("regime") or regime_from_reason or "").strip() or None

    pnl_pct = _safe_float(close_position.get("pnl_pct"))
    mfe_pct = _safe_float(close_position.get("mfe_pct"))
    mae_pct = _safe_float(close_position.get("mae_pct"))
    entry_premium = _safe_float(open_position.get("entry_premium"))
    exit_premium = _safe_float(close_position.get("exit_premium"))
    bars_held = int(float(close_position.get("bars_held") or 0))
    lots = int(float(open_position.get("lots") or 0)) if open_position.get("lots") is not None else None
    stop_loss_pct = _safe_float(open_position.get("stop_loss_pct"))
    target_pct = _safe_float(open_position.get("target_pct"))
    confidence = _safe_float(signal_doc.get("confidence"))

    result = "UNKNOWN"
    if pnl_pct is not None:
        if pnl_pct > 0:
            result = "WIN"
        elif pnl_pct < 0:
            result = "LOSS"
        else:
            result = "FLAT"

    return {
        "position_id": position_id,
        "signal_id": signal_id or None,
        "entry_strategy": strategy,
        "regime": regime,
        "direction": str(open_position.get("direction") or "").strip() or None,
        "entry_time": _iso_or_none(open_position.get("timestamp")),
        "exit_time": _iso_or_none(close_position.get("timestamp")),
        "trade_date_ist": str((docs.get("open_doc") or {}).get("trade_date_ist") or (docs.get("close_doc") or {}).get("trade_date_ist") or "").strip() or None,
        "entry_premium": entry_premium,
        "exit_premium": exit_premium,
        "pnl_pct": pnl_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "bars_held": bars_held,
        "lots": lots,
        "stop_loss_pct": stop_loss_pct,
        "target_pct": target_pct,
        "signal_confidence": confidence,
        "exit_reason": str(close_position.get("exit_reason") or "").strip() or None,
        "result": result,
        "entry_reason": str(open_position.get("reason") or "").strip() or None,
    }


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [trade["pnl_pct"] for trade in trades if trade.get("pnl_pct") is not None]
    mfes = [trade["mfe_pct"] for trade in trades if trade.get("mfe_pct") is not None]
    maes = [trade["mae_pct"] for trade in trades if trade.get("mae_pct") is not None]
    bars = [trade["bars_held"] for trade in trades if trade.get("bars_held") is not None]
    confidences = [trade["signal_confidence"] for trade in trades if trade.get("signal_confidence") is not None]
    winners = [value for value in pnls if value > 0]
    losers = [value for value in pnls if value < 0]
    flats = [value for value in pnls if value == 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    avg_mae_abs = abs(sum(maes) / len(maes)) if maes else None
    avg_mfe = (sum(mfes) / len(mfes)) if mfes else None
    profit_factor = None
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None

    return {
        "trades": len(trades),
        "wins": len(winners),
        "losses": len(losers),
        "flats": len(flats),
        "win_rate": (len(winners) / len(pnls)) if pnls else None,
        "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "median_pnl_pct": median(pnls) if pnls else None,
        "avg_winner_pct": (sum(winners) / len(winners)) if winners else None,
        "avg_loser_pct": (sum(losers) / len(losers)) if losers else None,
        "gross_profit_pct": gross_profit if winners else 0.0,
        "gross_loss_pct": -gross_loss if losers else 0.0,
        "profit_factor": profit_factor,
        "expectancy_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": (sum(maes) / len(maes)) if maes else None,
        "mfe_mae_ratio": ((avg_mfe / avg_mae_abs) if avg_mfe is not None and avg_mae_abs not in (None, 0.0) else None),
        "avg_bars_held": (sum(bars) / len(bars)) if bars else None,
        "avg_signal_confidence": (sum(confidences) / len(confidences)) if confidences else None,
    }


def _group_summary(trades: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for trade in trades:
        key = tuple(trade.get(name) for name in keys)
        grouped.setdefault(key, []).append(trade)

    rows: list[dict[str, Any]] = []
    for key, items in sorted(grouped.items(), key=lambda item: tuple("" if value is None else str(value) for value in item[0])):
        row = {name: key[idx] for idx, name in enumerate(keys)}
        row.update(_summarize_trades(items))
        rows.append(row)
    return rows


def _exit_reason_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        reason = str(trade.get("exit_reason") or "UNKNOWN")
        grouped.setdefault(reason, []).append(trade)
    rows: list[dict[str, Any]] = []
    for reason, items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        pnls = [trade["pnl_pct"] for trade in items if trade.get("pnl_pct") is not None]
        rows.append(
            {
                "exit_reason": reason,
                "trades": len(items),
                "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
            }
        )
    return rows


def build_evaluation(*, date_from: Optional[str], date_to: Optional[str], limit: int) -> dict[str, Any]:
    client = _mongo_client()
    db_name = str(os.getenv("MONGO_DB") or "trading_ai").strip() or "trading_ai"
    vote_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_VOTES") or "strategy_votes").strip() or "strategy_votes"
    signal_coll_name = str(os.getenv("MONGO_COLL_TRADE_SIGNALS") or "trade_signals").strip() or "trade_signals"
    position_coll_name = str(os.getenv("MONGO_COLL_STRATEGY_POSITIONS") or "strategy_positions").strip() or "strategy_positions"

    db = client[db_name]
    votes = db[vote_coll_name]
    signals = db[signal_coll_name]
    positions = db[position_coll_name]
    date_match = _date_filter(date_from=date_from, date_to=date_to)

    signal_map = _load_signal_map(signals, date_match=date_match)
    position_map = _load_positions(positions, date_match=date_match)
    trades = [
        trade
        for position_id, docs in position_map.items()
        for trade in [_trade_from_docs(position_id, docs, signal_map)]
        if trade is not None
    ]
    trades.sort(key=lambda item: (str(item.get("entry_time") or ""), str(item.get("position_id") or "")))

    open_positions = [
        {
            "position_id": position_id,
            "has_open": isinstance(docs.get("open"), dict),
            "has_close": isinstance(docs.get("close"), dict),
        }
        for position_id, docs in sorted(position_map.items())
        if not (isinstance(docs.get("open"), dict) and isinstance(docs.get("close"), dict))
    ]

    report = {
        "generated_at": isoformat_ist(),
        "db": db_name,
        "collections": {
            "strategy_votes": vote_coll_name,
            "trade_signals": signal_coll_name,
            "strategy_positions": position_coll_name,
        },
        "filters": {
            "date_from": date_from,
            "date_to": date_to,
        },
        "counts": {
            "votes": votes.count_documents(date_match),
            "signals": signals.count_documents(date_match),
            "position_events": positions.count_documents(date_match),
            "closed_trades": len(trades),
            "incomplete_positions": len(open_positions),
        },
        "overall": _summarize_trades(trades),
        "by_strategy_regime": _group_summary(trades, ["entry_strategy", "regime"]),
        "by_strategy": _group_summary(trades, ["entry_strategy"]),
        "by_regime": _group_summary(trades, ["regime"]),
        "by_direction": _group_summary(trades, ["direction"]),
        "by_exit_reason": _exit_reason_summary(trades),
        "incomplete_positions": open_positions[: max(1, int(limit))],
        "latest_trades": trades[-max(1, int(limit)) :],
    }
    client.close()
    return report


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Performance evaluation from persisted strategy history")
    parser.add_argument("--date-from", default=None, help="Inclusive IST trade_date_ist lower bound YYYY-MM-DD")
    parser.add_argument("--date-to", default=None, help="Inclusive IST trade_date_ist upper bound YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=10, help="Latest trades and incomplete positions to include")
    parser.add_argument("--output", default=None, help="Optional path to write the JSON evaluation report")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = build_evaluation(date_from=args.date_from, date_to=args.date_to, limit=int(args.limit))
    rendered = json.dumps(report, ensure_ascii=False, default=str, indent=2)
    if args.output:
        output_path = Path(str(args.output)).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
from contracts_app import TimestampSourceMode, isoformat_ist, parse_timestamp_to_ist
