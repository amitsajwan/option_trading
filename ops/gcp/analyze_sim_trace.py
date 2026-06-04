#!/usr/bin/env python3
"""Distil decision traces into a compact, LLM-readable per-component health report.

The strategy engine emits one rich decision trace per bar (DecisionTraceBuilder:
candidates with direction + entry_prob, per-gate veto, regime, and a
`market_structure` block — the bottoms/highs/breakouts lens). Those traces are
verbose (≈1 per minute of session); this tool reduces a run's worth of them to a
single digest small enough to read or hand to an LLM, answering:

  * ML entry  — is the entry model firing/discriminating, or saying "yes" to everything?
  * Direction — CE/PE split, which direction engine ran (composite vs consensus),
                and (if outcomes are joined) the directional hit-rate.
  * Gates     — what vetoes / blockers killed trades, as a histogram.
  * Structure — WHERE in the tape entries happened (near high/low, breakout vs fade,
                trend vs range, momentum aligned?) cross-tabbed with win/loss.
  * Per trade — the full cascade for each taken trade.

Inputs are JSONL files (one trace per line), so it works on both the ephemeral sim
export and the live decision-trace sink. It can also be imported and called with an
in-process list of traces (e.g. a finished replay's `decision_traces`).

Usage:
  python analyze_sim_trace.py --traces traces.jsonl [--trades trades.jsonl] \
      [--json out.json] [--md out.md]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from typing import Any, Iterable, Optional


# ── helpers ───────────────────────────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _dist(values: list[float]) -> dict[str, Any]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    vals_sorted = sorted(vals)
    return {
        "n": len(vals),
        "min": round(min(vals), 4),
        "median": round(statistics.median(vals), 4),
        "max": round(max(vals), 4),
        "mean": round(statistics.fmean(vals), 4),
    }


def _histogram(values: list[float], edges: list[float]) -> dict[str, int]:
    """Bucket values into [edges[i], edges[i+1]) ranges, plus a >=last bucket."""
    out: dict[str, int] = {}
    for i in range(len(edges) - 1):
        out[f"{edges[i]:g}-{edges[i+1]:g}"] = 0
    out[f">={edges[-1]:g}"] = 0
    for v in values:
        if v is None:
            continue
        placed = False
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                out[f"{edges[i]:g}-{edges[i+1]:g}"] += 1
                placed = True
                break
        if not placed and v >= edges[-1]:
            out[f">={edges[-1]:g}"] += 1
    return out


def _entry_candidate(trace: dict) -> Optional[dict]:
    """The ML_ENTRY (or selected) candidate from a trace."""
    cands = trace.get("candidates") if isinstance(trace.get("candidates"), list) else []
    selected = next((c for c in cands if isinstance(c, dict) and c.get("selected")), None)
    if selected is not None:
        return selected
    for c in cands:
        if isinstance(c, dict) and "ML_ENTRY" in str(c.get("strategy_name") or "").upper():
            return c
    return cands[0] if cands and isinstance(cands[0], dict) else None


def _entry_prob(cand: Optional[dict]) -> Optional[float]:
    if not cand:
        return None
    metrics = cand.get("metrics") if isinstance(cand.get("metrics"), dict) else {}
    for key in ("entry_prob", "confidence"):
        v = _num(metrics.get(key))
        if v is not None:
            return v
    return _num(cand.get("confidence"))


def _read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _minute_key(ts: Any) -> Optional[str]:
    """Normalise a timestamp to 'YYYY-MM-DDTHH:MM' for cross-source joins."""
    s = str(ts or "").strip()
    if len(s) >= 16 and s[10] in ("T", " "):
        return s[:10] + "T" + s[11:16]
    return None


def _fut_ohlc(snap: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Pull (high, low, close) from a snapshot in either flat or nested shape."""
    fb = snap.get("futures_bar")
    if not isinstance(fb, dict):
        payload = snap.get("payload") if isinstance(snap.get("payload"), dict) else {}
        inner = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        fb = inner.get("futures_bar") if isinstance(inner.get("futures_bar"), dict) else {}
    high = _num(fb.get("fut_high") or fb.get("high") or snap.get("fut_high"))
    low = _num(fb.get("fut_low") or fb.get("low") or snap.get("fut_low"))
    close = _num(fb.get("fut_close") or fb.get("close") or snap.get("fut_close"))
    if high is None:
        high = close
    if low is None:
        low = close
    return high, low, close


def verify_entry_label(
    traces: list[dict],
    snapshots: list[dict],
    *,
    horizon_minutes: int = 10,
    min_points: float = 50.0,
    prob_threshold: float = 0.65,
) -> dict[str, Any]:
    """Post-hoc check the entry model against its own label.

    The entry model predicts: "within `horizon_minutes`, BN futures moves >=
    `min_points` in EITHER direction" (forward high/low excursion vs entry price;
    see ml_pipeline_2/.../entry_move_oracle.py). For every bar where the model
    FIRED (entry_prob >= threshold) we look ahead and compute the realised
    excursion in points, then mark whether the predicted move actually happened.

    This isolates the entry model from direction/gates/exit: it answers ONLY
    "when it said 'a move is coming', did a move come?" — and reports the same for
    non-fired bars so the on-day separation (precision/recall) is visible.
    """
    # Ordered minute-indexed fut series with high/low for excursion.
    series: list[tuple[str, float, float, float]] = []
    for s in snapshots:
        mk = _minute_key(s.get("timestamp") or (s.get("session_context") or {}).get("timestamp"))
        high, low, close = _fut_ohlc(s)
        if mk and close is not None:
            series.append((mk, high, low, close))
    series.sort(key=lambda r: r[0])
    idx_by_minute = {mk: i for i, (mk, *_2) in enumerate(series)}

    def realised(mk: str) -> Optional[dict[str, Any]]:
        i = idx_by_minute.get(mk)
        if i is None:
            return None
        entry_px = series[i][3]
        end = min(len(series), i + 1 + max(1, int(horizon_minutes)))
        window = series[i + 1:end]
        if not window or entry_px <= 0:
            return None
        fwd_high = max(r[1] for r in window)
        fwd_low = min(r[2] for r in window)
        up_pts = fwd_high - entry_px
        down_pts = entry_px - fwd_low
        max_pts = max(up_pts, down_pts)
        return {
            "up_pts": round(up_pts, 1),
            "down_pts": round(down_pts, 1),
            "max_excursion_pts": round(max_pts, 1),
            "moved": bool(max_pts >= min_points),
            "side_of_move": "up" if up_pts >= down_pts else "down",
        }

    fired_total = fired_moved = 0
    notfired_total = notfired_moved = 0
    fired_excursions: list[float] = []
    detail: list[dict[str, Any]] = []
    for t in traces:
        cand = _entry_candidate(t)
        p = _entry_prob(cand)
        if p is None:
            continue
        mk = _minute_key(t.get("timestamp"))
        r = realised(mk) if mk else None
        if r is None:
            continue
        if p >= prob_threshold:
            fired_total += 1
            fired_excursions.append(r["max_excursion_pts"])
            if r["moved"]:
                fired_moved += 1
            detail.append({"time": mk, "entry_prob": round(p, 4), **r,
                           "direction": (cand or {}).get("direction"),
                           "outcome": t.get("final_outcome")})
        else:
            notfired_total += 1
            if r["moved"]:
                notfired_moved += 1

    precision = round(fired_moved / fired_total, 4) if fired_total else None
    base_rate_notfired = round(notfired_moved / notfired_total, 4) if notfired_total else None
    return {
        "label": {"horizon_minutes": horizon_minutes, "min_points": min_points,
                  "prob_threshold": prob_threshold,
                  "definition": "move >= min_points in EITHER direction within horizon"},
        "fired": {"n": fired_total, "moved": fired_moved, "precision": precision},
        "not_fired": {"n": notfired_total, "moved": notfired_moved, "move_rate": base_rate_notfired},
        "separation": (round((precision or 0) - (base_rate_notfired or 0), 4)
                       if precision is not None and base_rate_notfired is not None else None),
        "fired_excursion_pts": _dist(fired_excursions),
        "fired_detail": detail,
    }


# ── core analysis ──────────────────────────────────────────────────────────────
def analyze_traces(
    traces: Iterable[dict],
    trades: Optional[list[dict]] = None,
    *,
    snapshots: Optional[list[dict]] = None,
    entry_horizon_minutes: int = 10,
    entry_min_points: float = 50.0,
) -> dict[str, Any]:
    traces = [t for t in traces if isinstance(t, dict)]
    entry_threshold = 0.65

    # outcome lookup by (direction, strike) for the structure cross-tab.
    trade_outcomes: list[dict] = []
    for t in (trades or []):
        pnl = _num(t.get("pnl_pct"))
        if pnl is None:
            pos = t.get("payload", {}).get("position", {}) if isinstance(t.get("payload"), dict) else {}
            pnl = _num(pos.get("pnl_pct"))
        if pnl is not None:
            trade_outcomes.append({"pnl_pct": pnl})

    meta = {
        "n_traces": len(traces),
        "run_id": next((t.get("run_id") for t in traces if t.get("run_id")), None),
        "trade_date": next((t.get("trade_date_ist") for t in traces if t.get("trade_date_ist")), None),
        "engine_mode": Counter(str(t.get("engine_mode")) for t in traces).most_common(1)[0][0] if traces else None,
        "direction_source": dict(Counter(str(t.get("direction_source")) for t in traces if t.get("direction_source"))),
        "final_outcome": dict(Counter(str(t.get("final_outcome")) for t in traces)),
    }

    # ── ML entry health ──
    probs: list[float] = []
    fired = 0
    for t in traces:
        cand = _entry_candidate(t)
        p = _entry_prob(cand)
        if p is not None:
            probs.append(p)
            if p >= entry_threshold:
                fired += 1
    ml_entry = {
        "bars_with_entry_candidate": len(probs),
        "entry_prob": _dist(probs),
        "histogram": _histogram(probs, [0.0, 0.5, 0.65, 0.8, 0.9, 1.0]),
        "cleared_threshold": {"threshold": entry_threshold, "count": fired,
                              "pct_of_candidates": round(100.0 * fired / len(probs), 1) if probs else None},
    }

    # ── direction health ──
    taken = [t for t in traces if str(t.get("final_outcome")) == "entry_taken"]
    dir_counter: Counter = Counter()
    margins: list[float] = []
    for t in taken:
        cand = _entry_candidate(t)
        if cand:
            dir_counter[str(cand.get("direction") or "?")] += 1
            metrics = cand.get("metrics") if isinstance(cand.get("metrics"), dict) else {}
            for mk in ("entry_dir_margin", "margin", "direction_consensus_margin"):
                m = _num(metrics.get(mk))
                if m is not None:
                    margins.append(m)
                    break
    direction = {
        "taken": len(taken),
        "ce_pe": dict(dir_counter),
        "margin": _dist(margins),
        "direction_source": meta["direction_source"],
    }

    # ── gate / veto histogram ──
    blockers = Counter(str(t.get("primary_blocker_gate")) for t in traces if t.get("primary_blocker_gate"))
    veto_codes: Counter = Counter()
    for t in traces:
        for c in (t.get("candidates") or []):
            if isinstance(c, dict) and c.get("terminal_status") in ("blocked", "vetoed") and c.get("terminal_reason_code"):
                veto_codes[str(c.get("terminal_reason_code"))] += 1
    gates = {"primary_blocker_gate": dict(blockers.most_common()),
             "candidate_veto_reason": dict(veto_codes.most_common())}

    # ── market-structure read (taken trades) ──
    def _ms_field(t: dict, *path: str) -> Any:
        node: Any = t.get("market_structure") or {}
        for p in path:
            node = node.get(p) if isinstance(node, dict) else None
        return node

    structure = {
        "position_in_range": dict(Counter(_ms_field(t, "position_in_range", "label") or "n/a" for t in taken)),
        "breakout_state": dict(Counter(_ms_field(t, "breakout_state", "label") or "n/a" for t in taken)),
        "swing_structure": dict(Counter(_ms_field(t, "swing_pivots", "structure") or "n/a" for t in taken)),
        "momentum": dict(Counter(_ms_field(t, "momentum_alignment", "label") or "n/a" for t in taken)),
    }

    # ── per-trade cascade ──
    per_trade = []
    for t in taken:
        cand = _entry_candidate(t)
        per_trade.append({
            "time": t.get("timestamp"),
            "direction": (cand or {}).get("direction"),
            "entry_prob": _entry_prob(cand),
            "direction_source": t.get("direction_source"),
            "regime": (t.get("regime_context") or {}).get("regime") if isinstance(t.get("regime_context"), dict) else None,
            "position_in_range": _ms_field(t, "position_in_range", "label"),
            "range_position": _ms_field(t, "position_in_range", "range_position"),
            "breakout": _ms_field(t, "breakout_state", "label"),
            "swing": _ms_field(t, "swing_pivots", "structure"),
            "momentum": _ms_field(t, "momentum_alignment", "label"),
            "execution_path": t.get("execution_path"),
        })

    # ── entry-model label verification (needs forward fut prices) ──
    entry_label_check = None
    if snapshots:
        entry_label_check = verify_entry_label(
            traces, snapshots,
            horizon_minutes=entry_horizon_minutes,
            min_points=entry_min_points,
            prob_threshold=entry_threshold,
        )

    return {
        "meta": meta,
        "ml_entry": ml_entry,
        "entry_label_check": entry_label_check,
        "direction": direction,
        "gates": gates,
        "market_structure": structure,
        "trades_summary": {"n": len(trade_outcomes),
                           "wins": sum(1 for o in trade_outcomes if o["pnl_pct"] > 0),
                           "losses": sum(1 for o in trade_outcomes if o["pnl_pct"] <= 0),
                           "net_pnl_pct": round(sum(o["pnl_pct"] for o in trade_outcomes), 4)} if trade_outcomes else None,
        "per_trade": per_trade,
    }


# ── markdown render ─────────────────────────────────────────────────────────────
def render_markdown(report: dict[str, Any]) -> str:
    m = report["meta"]
    L: list[str] = []
    L.append(f"# Decision-trace digest - run `{m.get('run_id')}` ({m.get('trade_date')})")
    L.append("")
    L.append(f"- traces: **{m.get('n_traces')}** | engine: `{m.get('engine_mode')}` | outcomes: {m.get('final_outcome')}")
    L.append(f"- direction_source: **{m.get('direction_source') or 'n/a'}**  (composite=heuristic, consensus/direction_ml=stage-2 model)")
    ts = report.get("trades_summary")
    if ts:
        L.append(f"- trades: {ts['n']} ({ts['wins']}W/{ts['losses']}L) net **{ts['net_pnl_pct']:+.2%}**" if isinstance(ts['net_pnl_pct'], float) else f"- trades: {ts['n']}")
    L.append("")

    me = report["ml_entry"]
    L.append("## ML entry health")
    L.append(f"- entry_prob over {me['bars_with_entry_candidate']} bars: {me['entry_prob']}")
    L.append(f"- histogram: {me['histogram']}")
    ct = me["cleared_threshold"]
    L.append(f"- cleared {ct['threshold']}: {ct['count']} ({ct['pct_of_candidates']}% of candidates) "
             f"-> {'WARN: likely a near-constant YES (not discriminating)' if (ct['pct_of_candidates'] or 0) > 90 else 'discriminating'}")
    L.append("")

    elc = report.get("entry_label_check")
    if elc:
        lab = elc["label"]
        fired, nf = elc["fired"], elc["not_fired"]
        L.append(f"## Entry-model label check (did the predicted move actually happen?)")
        L.append(f"- label: **>= {lab['min_points']:g} pts in either direction within {lab['horizon_minutes']}min** "
                 f"(fire when prob >= {lab['prob_threshold']})")
        L.append(f"- FIRED: {fired['n']} bars, move happened {fired['moved']} -> precision **{fired['precision']}**")
        L.append(f"- NOT fired: {nf['n']} bars, move happened {nf['moved']} -> base move-rate {nf['move_rate']}")
        L.append(f"- separation (precision - base): **{elc['separation']}** (>0 means the model adds signal on this day)")
        L.append(f"- realised excursion on fired bars (pts): {elc['fired_excursion_pts']}")
        L.append("")

    d = report["direction"]
    L.append("## Direction health")
    L.append(f"- taken: {d['taken']} | CE/PE: {d['ce_pe']} | margin: {d['margin']}")
    L.append(f"- direction_source: {d['direction_source'] or 'n/a'}")
    L.append("")

    g = report["gates"]
    L.append("## Gates / vetoes")
    L.append(f"- primary_blocker_gate: {g['primary_blocker_gate'] or '(none)'}")
    L.append(f"- candidate_veto_reason: {g['candidate_veto_reason'] or '(none)'}")
    L.append("")

    s = report["market_structure"]
    L.append("## Market-structure read (taken trades)")
    L.append(f"- position_in_range: {s['position_in_range']}")
    L.append(f"- breakout_state: {s['breakout_state']}")
    L.append(f"- swing_structure: {s['swing_structure']}")
    L.append(f"- momentum: {s['momentum']}")
    L.append("")

    L.append("## Per-trade cascade")
    for i, pt in enumerate(report["per_trade"]):
        prob = pt["entry_prob"]
        prob_s = f"{prob:.3f}" if isinstance(prob, float) else "?"
        L.append(f"{i}. {pt.get('time')} **{pt.get('direction')}** prob={prob_s} "
                 f"[{pt.get('regime')}] src={pt.get('direction_source')} | "
                 f"range={pt.get('position_in_range')}({pt.get('range_position')}) "
                 f"breakout={pt.get('breakout')} swing={pt.get('swing')} mom={pt.get('momentum')}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--traces", required=True, help="JSONL of decision traces (one per line)")
    ap.add_argument("--trades", default=None, help="optional JSONL of trades/positions for win/loss join")
    ap.add_argument("--snapshots", default=None,
                    help="optional JSONL of fut snapshots (timestamp + fut_high/low/close) for entry-label verification")
    ap.add_argument("--entry-horizon", type=int, default=10, help="entry-label horizon minutes (deployed model=10)")
    ap.add_argument("--entry-min-points", type=float, default=50.0, help="entry-label move threshold in points (deployed model=50)")
    ap.add_argument("--json", dest="json_out", default=None, help="write full report JSON here")
    ap.add_argument("--md", dest="md_out", default=None, help="write markdown digest here")
    args = ap.parse_args()

    traces = _read_jsonl(args.traces)
    trades = _read_jsonl(args.trades) if args.trades else None
    snapshots = _read_jsonl(args.snapshots) if args.snapshots else None
    if not traces:
        print(f"no traces found in {args.traces}", file=sys.stderr)
        return 2

    report = analyze_traces(
        traces, trades,
        snapshots=snapshots,
        entry_horizon_minutes=args.entry_horizon,
        entry_min_points=args.entry_min_points,
    )
    md = render_markdown(report)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
