import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class GuardConfig:
    max_unmatched_intent_share: float = 0.05
    max_side_mismatch_share: float = 0.01
    max_consecutive_losses: int = 4
    max_drawdown: float = 0.30


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def _load_jsonl(path: Path) -> List[Dict[str, object]]:
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
    return rows


def _pick_side(record: Dict[str, object]) -> Optional[str]:
    position = record.get("position")
    if isinstance(position, dict):
        side = str(position.get("side", "")).upper().strip()
        if side in {"CE", "PE"}:
            return side
    action = str(record.get("action", "")).upper().strip()
    if action == "BUY_CE":
        return "CE"
    if action == "BUY_PE":
        return "PE"
    return None


def _pick_order_kind(record: Dict[str, object]) -> Optional[str]:
    et = str(record.get("event_type", "")).upper().strip()
    if et == "ENTRY":
        return "OPEN"
    if et == "EXIT":
        return "CLOSE"
    return None


def _pick_option_symbol(record: Dict[str, object]) -> Optional[str]:
    rt = record.get("position_runtime")
    if isinstance(rt, dict):
        sym = rt.get("option_symbol")
        if isinstance(sym, str) and sym:
            return sym
    pos = record.get("position")
    if isinstance(pos, dict):
        sym = pos.get("option_symbol")
        if isinstance(sym, str) and sym:
            return sym
    return None


def _make_intent_id(
    *,
    timestamp: str,
    side: str,
    order_kind: str,
    option_symbol: Optional[str],
    source: str,
) -> str:
    seed = f"{timestamp}|{side}|{order_kind}|{option_symbol or ''}|{source}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return f"intent_{digest[:20]}"


def _normalize_intent(record: Dict[str, object], source: str, index: int) -> Optional[Dict[str, object]]:
    side = _pick_side(record)
    order_kind = _pick_order_kind(record)
    if side is None or order_kind is None:
        return None

    timestamp = str(record.get("timestamp", ""))
    if not timestamp:
        return None
    option_symbol = _pick_option_symbol(record)
    runtime = record.get("position_runtime")
    qty = _safe_float(runtime.get("qty")) if isinstance(runtime, dict) else float("nan")
    prices = record.get("prices")
    if isinstance(prices, dict):
        key = "opt_0_ce_close" if side == "CE" else "opt_0_pe_close"
        price_hint = _safe_float(prices.get(key))
    else:
        price_hint = float("nan")
    intent_id = _make_intent_id(
        timestamp=timestamp,
        side=side,
        order_kind=order_kind,
        option_symbol=option_symbol,
        source=source,
    )
    return {
        "intent_id": intent_id,
        "event_index": int(index),
        "timestamp": timestamp,
        "side": side,
        "order_kind": order_kind,
        "option_symbol": option_symbol,
        "quantity": float(qty) if np.isfinite(qty) else None,
        "price_hint": float(price_hint) if np.isfinite(price_hint) else None,
        "source": source,
    }


def _normalize_fill(record: Dict[str, object], source: str, index: int) -> Optional[Dict[str, object]]:
    # Explicit fill record path.
    if "intent_id" in record and "fill_price" in record:
        side = str(record.get("side", "")).upper().strip()
        if side not in {"CE", "PE"}:
            return None
        order_kind = str(record.get("order_kind", "")).upper().strip()
        if order_kind not in {"OPEN", "CLOSE"}:
            return None
        ts = str(record.get("fill_timestamp", record.get("timestamp", "")))
        if not ts:
            return None
        fill_price = _safe_float(record.get("fill_price"))
        if not np.isfinite(fill_price) or fill_price <= 0:
            return None
        fill_id = str(record.get("fill_id", "")) or f"fill_{record['intent_id']}_{index}"
        qty = _safe_float(record.get("filled_qty"))
        ret = _safe_float(record.get("return_pct"))
        return {
            "fill_id": fill_id,
            "intent_id": str(record["intent_id"]),
            "fill_timestamp": ts,
            "side": side,
            "order_kind": order_kind,
            "fill_price": float(fill_price),
            "filled_qty": float(qty) if np.isfinite(qty) else None,
            "return_pct": float(ret) if np.isfinite(ret) else None,
            "source": source,
        }

    # Decision-event-as-fill path.
    side = _pick_side(record)
    order_kind = _pick_order_kind(record)
    if side is None or order_kind is None:
        return None
    ts = str(record.get("timestamp", ""))
    if not ts:
        return None
    option_symbol = _pick_option_symbol(record)
    intent_id = _make_intent_id(
        timestamp=ts,
        side=side,
        order_kind=order_kind,
        option_symbol=option_symbol,
        source=source,
    )

    prices = record.get("prices")
    fill_price = float("nan")
    if isinstance(prices, dict):
        key = "opt_0_ce_close" if side == "CE" else "opt_0_pe_close"
        fill_price = _safe_float(prices.get(key))
    if not np.isfinite(fill_price) or fill_price <= 0:
        return None

    runtime = record.get("position_runtime")
    qty = _safe_float(runtime.get("qty")) if isinstance(runtime, dict) else float("nan")
    ret = float("nan")
    if order_kind == "CLOSE" and isinstance(runtime, dict):
        entry_price = _safe_float(runtime.get("entry_price"))
        if np.isfinite(entry_price) and entry_price > 0:
            ret = (float(fill_price) - float(entry_price)) / float(entry_price)
    return {
        "fill_id": f"fill_{intent_id}",
        "intent_id": intent_id,
        "fill_timestamp": ts,
        "side": side,
        "order_kind": order_kind,
        "fill_price": float(fill_price),
        "filled_qty": float(qty) if np.isfinite(qty) else None,
        "return_pct": float(ret) if np.isfinite(ret) else None,
        "source": source,
    }


def _dedupe_rows(rows: Sequence[Dict[str, object]], key: str) -> Tuple[List[Dict[str, object]], int]:
    seen = set()
    out: List[Dict[str, object]] = []
    dropped = 0
    for row in rows:
        marker = row.get(key)
        if marker in seen:
            dropped += 1
            continue
        seen.add(marker)
        out.append(dict(row))
    return out, int(dropped)


def build_order_intents(events: Sequence[Dict[str, object]], source: str = "decision_events") -> Dict[str, object]:
    rows: List[Dict[str, object]] = []
    for i, event in enumerate(events):
        row = _normalize_intent(event, source=source, index=i)
        if row is not None:
            rows.append(row)
    deduped, dropped = _dedupe_rows(rows, key="intent_id")
    frame = pd.DataFrame(deduped)
    return {
        "raw_count": int(len(rows)),
        "deduped_count": int(len(deduped)),
        "duplicate_count": int(dropped),
        "intents": frame,
    }


def build_fills(events: Sequence[Dict[str, object]], source: str = "fill_events") -> Dict[str, object]:
    rows: List[Dict[str, object]] = []
    for i, event in enumerate(events):
        row = _normalize_fill(event, source=source, index=i)
        if row is not None:
            rows.append(row)
    deduped, dropped = _dedupe_rows(rows, key="fill_id")
    frame = pd.DataFrame(deduped)
    return {
        "raw_count": int(len(rows)),
        "deduped_count": int(len(deduped)),
        "duplicate_count": int(dropped),
        "fills": frame,
    }


def reconcile_intents_fills(intents: pd.DataFrame, fills: pd.DataFrame, sample_limit: int = 25) -> Dict[str, object]:
    if len(intents) == 0:
        return {
            "matched_intents": 0,
            "unmatched_intents": 0,
            "unmatched_fills": int(len(fills)),
            "side_mismatch": 0,
            "kind_mismatch": 0,
            "mismatch_samples": [],
        }

    fills_by_intent: Dict[str, List[Dict[str, object]]] = {}
    for row in fills.to_dict(orient="records"):
        key = str(row.get("intent_id", ""))
        fills_by_intent.setdefault(key, []).append(row)

    matched = 0
    unmatched_intents = 0
    side_mismatch = 0
    kind_mismatch = 0
    mismatch_samples: List[Dict[str, object]] = []
    used_fill_ids = set()

    for intent in intents.to_dict(orient="records"):
        intent_id = str(intent.get("intent_id", ""))
        fill_rows = fills_by_intent.get(intent_id, [])
        if not fill_rows:
            unmatched_intents += 1
            if len(mismatch_samples) < int(sample_limit):
                mismatch_samples.append({"intent_id": intent_id, "issue": "missing_fill"})
            continue
        fill = fill_rows[0]
        used_fill_ids.add(str(fill.get("fill_id", "")))
        matched += 1
        if str(fill.get("side", "")) != str(intent.get("side", "")):
            side_mismatch += 1
            if len(mismatch_samples) < int(sample_limit):
                mismatch_samples.append(
                    {"intent_id": intent_id, "issue": "side_mismatch", "intent_side": intent.get("side"), "fill_side": fill.get("side")}
                )
        if str(fill.get("order_kind", "")) != str(intent.get("order_kind", "")):
            kind_mismatch += 1
            if len(mismatch_samples) < int(sample_limit):
                mismatch_samples.append(
                    {
                        "intent_id": intent_id,
                        "issue": "order_kind_mismatch",
                        "intent_kind": intent.get("order_kind"),
                        "fill_kind": fill.get("order_kind"),
                    }
                )

    unmatched_fills = 0
    for row in fills.to_dict(orient="records"):
        fill_id = str(row.get("fill_id", ""))
        if fill_id not in used_fill_ids:
            unmatched_fills += 1
            if len(mismatch_samples) < int(sample_limit):
                mismatch_samples.append({"fill_id": fill_id, "issue": "orphan_fill"})

    return {
        "matched_intents": int(matched),
        "unmatched_intents": int(unmatched_intents),
        "unmatched_fills": int(unmatched_fills),
        "side_mismatch": int(side_mismatch),
        "kind_mismatch": int(kind_mismatch),
        "mismatch_samples": mismatch_samples,
    }


def _max_consecutive_losses(returns: Sequence[float]) -> int:
    best = 0
    run = 0
    for val in returns:
        if float(val) < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return int(best)


def _max_drawdown_from_returns(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= 1.0 + float(ret)
        peak = max(peak, equity)
        dd = (equity / peak) - 1.0
        max_dd = min(max_dd, dd)
    return float(max_dd)


def evaluate_runtime_guards(
    *,
    intents: pd.DataFrame,
    fills: pd.DataFrame,
    reconciliation: Dict[str, object],
    guard_cfg: GuardConfig,
) -> Dict[str, object]:
    intents_total = int(len(intents))
    unmatched = int(reconciliation.get("unmatched_intents", 0))
    side_mismatch = int(reconciliation.get("side_mismatch", 0))

    unmatched_share = (float(unmatched) / float(intents_total)) if intents_total > 0 else 0.0
    side_mismatch_share = (float(side_mismatch) / float(intents_total)) if intents_total > 0 else 0.0

    close_fills = fills[fills["order_kind"] == "CLOSE"].copy() if len(fills) else fills
    returns = pd.to_numeric(close_fills.get("return_pct", pd.Series(dtype=float)), errors="coerce").dropna().astype(float).tolist()

    max_consec_losses = _max_consecutive_losses(returns)
    max_dd = _max_drawdown_from_returns(returns)

    alerts: List[Dict[str, object]] = []
    if unmatched_share > float(guard_cfg.max_unmatched_intent_share):
        alerts.append(
            {
                "type": "unmatched_intent_share",
                "severity": "critical",
                "value": unmatched_share,
                "threshold": float(guard_cfg.max_unmatched_intent_share),
                "message": "Unmatched intent share exceeds configured guard.",
            }
        )
    if side_mismatch_share > float(guard_cfg.max_side_mismatch_share):
        alerts.append(
            {
                "type": "side_mismatch_share",
                "severity": "critical",
                "value": side_mismatch_share,
                "threshold": float(guard_cfg.max_side_mismatch_share),
                "message": "Side mismatch share exceeds configured guard.",
            }
        )
    if max_consec_losses >= int(guard_cfg.max_consecutive_losses):
        alerts.append(
            {
                "type": "consecutive_losses",
                "severity": "critical",
                "value": int(max_consec_losses),
                "threshold": int(guard_cfg.max_consecutive_losses),
                "message": "Consecutive loss streak crossed guard threshold.",
            }
        )
    if abs(float(max_dd)) >= float(guard_cfg.max_drawdown):
        alerts.append(
            {
                "type": "drawdown",
                "severity": "critical",
                "value": float(max_dd),
                "threshold": -float(guard_cfg.max_drawdown),
                "message": "Drawdown crossed guard threshold.",
            }
        )

    return {
        "kill_switch": bool(len(alerts) > 0),
        "status": "halt" if len(alerts) > 0 else "ok",
        "metrics": {
            "intents_total": intents_total,
            "close_fills_total": int(len(close_fills)),
            "unmatched_intent_share": float(unmatched_share),
            "side_mismatch_share": float(side_mismatch_share),
            "max_consecutive_losses": int(max_consec_losses),
            "max_drawdown": float(max_dd),
        },
        "thresholds": asdict(guard_cfg),
        "alerts": alerts,
    }


def run_order_intent_runtime(
    decision_events: Sequence[Dict[str, object]],
    fill_events: Optional[Sequence[Dict[str, object]]] = None,
    guard_cfg: Optional[GuardConfig] = None,
) -> Dict[str, object]:
    effective_guard = guard_cfg or GuardConfig()
    fills_source_events = list(fill_events) if fill_events is not None else list(decision_events)

    intent_out = build_order_intents(decision_events, source="decision_events")
    fill_out = build_fills(fills_source_events, source=("fill_events" if fill_events is not None else "decision_events"))
    intents_df = intent_out["intents"]
    fills_df = fill_out["fills"]

    reconciliation = reconcile_intents_fills(intents_df, fills_df)
    guards = evaluate_runtime_guards(
        intents=intents_df,
        fills=fills_df,
        reconciliation=reconciliation,
        guard_cfg=effective_guard,
    )

    return {
        "created_at_ist": datetime.now(IST).isoformat(),
        "task": "T33",
        "status": "completed",
        "order_intent_contract": {
            "version": "v1",
            "required_fields": [
                "intent_id",
                "timestamp",
                "side",
                "order_kind",
                "option_symbol",
                "quantity",
                "price_hint",
                "source",
            ],
            "idempotency_key": "intent_id",
        },
        "decision_events_total": int(len(decision_events)),
        "fill_events_total": int(len(fills_source_events)),
        "intent_counts": {
            "raw": int(intent_out["raw_count"]),
            "deduped": int(intent_out["deduped_count"]),
            "duplicates_dropped": int(intent_out["duplicate_count"]),
        },
        "fill_counts": {
            "raw": int(fill_out["raw_count"]),
            "deduped": int(fill_out["deduped_count"]),
            "duplicates_dropped": int(fill_out["duplicate_count"]),
        },
        "reconciliation": reconciliation,
        "runtime_guards": guards,
    }


def _summary_markdown(report: Dict[str, object]) -> str:
    recon = report["reconciliation"]
    guards = report["runtime_guards"]
    lines = [
        "# T33 Order Intent, Reconciliation, and Runtime Guards Summary",
        "",
        f"- Created (IST): `{report['created_at_ist']}`",
        f"- Decision events: `{report['decision_events_total']}`",
        f"- Intent (raw/deduped): `{report['intent_counts']['raw']}/{report['intent_counts']['deduped']}`",
        f"- Fill (raw/deduped): `{report['fill_counts']['raw']}/{report['fill_counts']['deduped']}`",
        "",
        "## Reconciliation",
        f"- Matched intents: `{recon['matched_intents']}`",
        f"- Unmatched intents: `{recon['unmatched_intents']}`",
        f"- Unmatched fills: `{recon['unmatched_fills']}`",
        f"- Side mismatch: `{recon['side_mismatch']}`",
        f"- Order-kind mismatch: `{recon['kind_mismatch']}`",
        "",
        "## Runtime Guards",
        f"- Status: `{guards['status']}`",
        f"- Kill switch: `{guards['kill_switch']}`",
        f"- Unmatched intent share: `{guards['metrics']['unmatched_intent_share']}`",
        f"- Max consecutive losses: `{guards['metrics']['max_consecutive_losses']}`",
        f"- Max drawdown: `{guards['metrics']['max_drawdown']}`",
        f"- Alerts: `{len(guards['alerts'])}`",
    ]
    return "\n".join(lines) + "\n"


def run_cli(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T33 order-intent contract + reconciliation + runtime guards")
    parser.add_argument("--decisions-jsonl", default="ml_pipeline/artifacts/t33_paper_capital_events_actual.jsonl")
    parser.add_argument("--fills-jsonl", default=None)
    parser.add_argument("--max-unmatched-intent-share", type=float, default=0.05)
    parser.add_argument("--max-side-mismatch-share", type=float, default=0.01)
    parser.add_argument("--max-consecutive-losses", type=int, default=4)
    parser.add_argument("--max-drawdown", type=float, default=0.30)
    parser.add_argument("--report-out", default="ml_pipeline/artifacts/t33_order_runtime_report.json")
    parser.add_argument("--summary-out", default="ml_pipeline/artifacts/t33_order_runtime_summary.md")
    parser.add_argument("--intents-out", default="ml_pipeline/artifacts/t33_order_intents.parquet")
    parser.add_argument("--fills-out", default="ml_pipeline/artifacts/t33_order_fills.parquet")
    args = parser.parse_args(list(argv) if argv is not None else None)

    decisions_path = Path(args.decisions_jsonl)
    if not decisions_path.exists():
        print(f"ERROR: decisions jsonl not found: {decisions_path}")
        return 2
    fills_path = Path(args.fills_jsonl) if args.fills_jsonl else None
    if fills_path is not None and not fills_path.exists():
        print(f"ERROR: fills jsonl not found: {fills_path}")
        return 2

    decision_events = _load_jsonl(decisions_path)
    fill_events = _load_jsonl(fills_path) if fills_path is not None else None

    guard_cfg = GuardConfig(
        max_unmatched_intent_share=float(args.max_unmatched_intent_share),
        max_side_mismatch_share=float(args.max_side_mismatch_share),
        max_consecutive_losses=max(1, int(args.max_consecutive_losses)),
        max_drawdown=max(0.0, float(args.max_drawdown)),
    )
    report = run_order_intent_runtime(decision_events=decision_events, fill_events=fill_events, guard_cfg=guard_cfg)

    report_out = Path(args.report_out)
    summary_out = Path(args.summary_out)
    intents_out = Path(args.intents_out)
    fills_out = Path(args.fills_out)
    for p in (report_out, summary_out, intents_out, fills_out):
        p.parent.mkdir(parents=True, exist_ok=True)

    # Persist normalized intents/fills for replay-safe audit.
    intent_df = build_order_intents(decision_events, source="decision_events")["intents"]
    fill_df = build_fills(fill_events if fill_events is not None else decision_events, source=("fill_events" if fill_events is not None else "decision_events"))["fills"]
    intent_df.to_parquet(intents_out, index=False)
    fill_df.to_parquet(fills_out, index=False)

    report_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary_out.write_text(_summary_markdown(report), encoding="utf-8")

    print(f"Intents (deduped): {report['intent_counts']['deduped']}")
    print(f"Fills (deduped): {report['fill_counts']['deduped']}")
    print(f"Matched intents: {report['reconciliation']['matched_intents']}")
    print(f"Kill switch: {report['runtime_guards']['kill_switch']}")
    print(f"Report: {report_out}")
    print(f"Summary: {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
