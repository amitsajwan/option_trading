"""Distil decision traces into a compact, LLM-readable per-component digest.

Core analysis lives here (shipped inside strategy_app, so it is importable from
both the strategy engine image and the dashboard image that runs the ops-sim).
``ops/gcp/analyze_sim_trace.py`` is a thin CLI wrapper around these functions.

The strategy engine emits one rich decision trace per bar (DecisionTraceBuilder:
candidates with direction + entry_prob, per-gate veto, regime, and a
`market_structure` block — the bottoms/highs/breakouts lens). This reduces a run's
worth of them to a single digest small enough to read or hand to an LLM:

  * ML entry  — is the entry model firing/discriminating, or saying "yes" always?
  * Entry label — for every bar the model fired, did the >=Npts/Tmin move happen?
  * Direction — CE/PE split, which direction engine ran (composite vs consensus).
  * Gates     — what vetoes / blockers killed trades, as a histogram.
  * Structure — WHERE in the tape entries happened, cross-tabbed with win/loss.
  * Per trade — the full cascade for each taken trade.
"""
from __future__ import annotations

import statistics
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
    n_bars = len(traces)
    ml_entry = {
        # Bars where the entry model FIRED (produced a vote, prob >= threshold).
        "bars_fired": len(probs),
        # Total bars in the session (the honest denominator — declined bars included).
        "bars_total": n_bars,
        # Fire rate over the whole session. The model declines on the rest — this is
        # the real discrimination, NOT visible if you only look at fired bars.
        "fire_rate_pct": round(100.0 * len(probs) / n_bars, 1) if n_bars else None,
        "entry_prob_when_fired": _dist(probs),
        "histogram": _histogram(probs, [0.0, 0.5, 0.65, 0.8, 0.9, 1.0]),
        "note": "entry_prob distribution is over FIRED bars only; declined-bar probs "
                "(<threshold) are not yet captured in the trace.",
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
    # Direction decision card — read the consolidated trace["direction"] block so
    # "why this side / why no direction veto" is answerable from the digest alone.
    def _dir(t: dict) -> dict:
        d = t.get("direction")
        return d if isinstance(d, dict) else {}

    ce_probs = [_num(_dir(t).get("ml_ce_prob")) for t in taken]
    ce_probs = [p for p in ce_probs if p is not None]
    bulls = [_num((_dir(t).get("evidence") or {}).get("bull_score")) for t in taken]
    bears = [_num((_dir(t).get("evidence") or {}).get("bear_score")) for t in taken]
    graded = sum(1 for t in taken if _dir(t).get("grade_evaluated"))
    modes = Counter(str(_dir(t).get("mode")) for t in taken if _dir(t))

    # ── Direction-model health over ALL bars (not just taken) ──────────────────
    # The model runs every bar; verifying it on the whole population is what makes
    # "is direction working?" clear. would_be_pe counts bars where ce_prob < 0.5 —
    # i.e. the model DID signal PE, even if none were taken that day. A tiny spread
    # (max-min) means the model is ~flat → no real directional signal on this run.
    dir_all = [_num(_dir(t).get("ml_ce_prob")) for t in traces if _dir(t).get("ml_ce_prob") is not None]
    dir_all = [p for p in dir_all if p is not None]
    would_ce = sum(1 for p in dir_all if p >= 0.5)
    would_pe = sum(1 for p in dir_all if p < 0.5)
    spread = round(max(dir_all) - min(dir_all), 4) if dir_all else None
    direction = {
        "taken": len(taken),
        "ce_pe": dict(dir_counter),
        "margin": _dist(margins),
        "direction_source": meta["direction_source"],
        "mode": dict(modes),
        "ml_ce_prob": _dist(ce_probs),
        "evidence_bull": _dist([b for b in bulls if b is not None]),
        "evidence_bear": _dist([b for b in bears if b is not None]),
        "grade_evaluated_count": graded,
        "grade_coverage": (round(graded / len(taken), 3) if taken else None),
        # Whole-run view of the direction model:
        "all_bars": {
            "n": len(dir_all),
            "ml_ce_prob": _dist(dir_all),
            "would_be_ce": would_ce,
            "would_be_pe": would_pe,
            "ce_prob_spread": spread,
            # Flat output across the whole session => effectively no directional signal.
            "degenerate": bool(spread is not None and spread < 0.05),
        },
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
        dd = _dir(t)
        ev = dd.get("evidence") or {}
        per_trade.append({
            "time": t.get("timestamp"),
            "direction": (cand or {}).get("direction"),
            "entry_prob": _entry_prob(cand),
            "direction_source": t.get("direction_source"),
            "ml_ce_prob": _num(dd.get("ml_ce_prob")),
            "bull": _num(ev.get("bull_score")),
            "bear": _num(ev.get("bear_score")),
            "grade": dd.get("grade"),
            "grade_evaluated": dd.get("grade_evaluated"),
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
    L.append(f"- FIRE RATE: {me['bars_fired']}/{me['bars_total']} bars = **{me['fire_rate_pct']}%** "
             f"(model declined on the other {me['bars_total'] - me['bars_fired']})")
    L.append(f"- entry_prob when fired: {me['entry_prob_when_fired']}")
    L.append(f"- histogram (fired bars): {me['histogram']}")
    L.append(f"- note: {me['note']}")
    L.append("")

    elc = report.get("entry_label_check")
    if elc:
        lab = elc["label"]
        fired, nf = elc["fired"], elc["not_fired"]
        L.append("## Entry-model label check (did the predicted move actually happen?)")
        L.append(f"- label: **>= {lab['min_points']:g} pts in either direction within {lab['horizon_minutes']}min** "
                 f"(fire when prob >= {lab['prob_threshold']})")
        L.append(f"- FIRED: {fired['n']} bars, move happened {fired['moved']} -> precision **{fired['precision']}**")
        L.append(f"- NOT fired: {nf['n']} bars, move happened {nf['moved']} -> base move-rate {nf['move_rate']}")
        L.append(f"- separation (precision - base): **{elc['separation']}** (>0 means the model adds signal on this day)")
        L.append(f"- realised excursion on fired bars (pts): {elc['fired_excursion_pts']}")
        L.append("")

    d = report["direction"]
    ab = d.get("all_bars") or {}
    L.append("## Direction health")
    L.append(f"- ALL BARS ({ab.get('n')}): ml_ce_prob {ab.get('ml_ce_prob')} | spread {ab.get('ce_prob_spread')}")
    L.append(f"- would-be CE/PE over all bars: {ab.get('would_be_ce')} / {ab.get('would_be_pe')}"
             + ("  -> WARN: model produced NO PE signal this run" if ab.get('would_be_pe') == 0 else ""))
    if ab.get("degenerate"):
        L.append(f"  -> WARN: DEGENERATE — ce_prob spread {ab.get('ce_prob_spread')} < 0.05; model ~flat, no real directional signal this run")
    L.append(f"- taken: {d['taken']} | CE/PE taken: {d['ce_pe']} | mode: {d.get('mode')} | source: {d['direction_source'] or 'n/a'}")
    L.append(f"- evidence bull: {d.get('evidence_bull')} | bear: {d.get('evidence_bear')}")
    gc = d.get("grade_coverage")
    L.append(f"- grade coverage: {d.get('grade_evaluated_count')}/{d['taken']} graded "
             + ("-> WARN: GOOD/OK/BAD grader BYPASSED (direction vetoes inactive in this mode)" if gc == 0 else ""))
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
                 f"[{pt.get('regime')}] src={pt.get('direction_source')} "
                 f"ce_prob={pt.get('ml_ce_prob')} bull={pt.get('bull')} bear={pt.get('bear')} "
                 f"grade={pt.get('grade')}(eval={pt.get('grade_evaluated')}) | "
                 f"range={pt.get('position_in_range')}({pt.get('range_position')}) "
                 f"breakout={pt.get('breakout')} swing={pt.get('swing')} mom={pt.get('momentum')}")
    return "\n".join(L)


def render_decision_card(trace: dict) -> str:
    """Render ONE decision completely — 'analysing an entry pass/fail, at one go'.

    Linearises a single bar's trace into a self-contained card: the entry-model
    prob, the full direction decision (mode/prob/evidence/grade + whether the grader
    even ran), the market-structure read, the ordered gate cascade with pass/veto and
    the values each gate saw, and the final outcome/blocker — so no engine grep or
    cross-referencing is needed to explain why this entry was taken or skipped.
    """
    cand = _entry_candidate(trace)
    d = trace.get("direction") if isinstance(trace.get("direction"), dict) else {}
    ev = d.get("evidence") or {}
    ms = trace.get("market_structure") or {}
    reg = trace.get("regime_context") or {}

    def _ms(*p):
        n = ms
        for k in p:
            n = n.get(k) if isinstance(n, dict) else None
        return n

    outcome = str(trace.get("final_outcome") or "?")
    blocker = trace.get("primary_blocker_gate")
    L: list[str] = []
    L.append(f"=== {trace.get('timestamp')}  OUTCOME={outcome.upper()}"
             + (f"  BLOCKER={blocker}" if blocker else "") + " ===")
    prob = _entry_prob(cand)
    L.append(f"  ENTRY    : prob={prob:.3f}" if isinstance(prob, float) else "  ENTRY    : prob=?")
    L.append(f"  DIRECTION: mode={d.get('mode')} chosen={d.get('chosen') or (cand or {}).get('direction')} "
             f"src={d.get('source')} ml_ce_prob={d.get('ml_ce_prob')} margin={d.get('margin')}")
    L.append(f"             evidence bull={ev.get('bull_score')} bear={ev.get('bear_score')}  "
             f"grade={d.get('grade')} tier={d.get('tier')} "
             f"grade_evaluated={d.get('grade_evaluated')}"
             + ("  <- grader BYPASSED (no thin-margin/chop/iv-skew veto)" if d.get('grade_evaluated') is False else ""))
    L.append(f"  REGIME   : {reg.get('regime')} (conf {reg.get('confidence')}) {reg.get('reason') or ''}")
    L.append(f"  STRUCTURE: range={_ms('position_in_range','label')}({_ms('position_in_range','range_position')}) "
             f"breakout={_ms('breakout_state','label')} swing={_ms('swing_pivots','structure')} "
             f"mom={_ms('momentum_alignment','label')}")
    # Gate cascade: flow gates + the entry candidate's ordered gates.
    rows: list = list(trace.get("flow_gates") or [])
    if isinstance(cand, dict):
        rows += list(cand.get("ordered_gates") or [])
    L.append("  GATES    :")
    for g in rows:
        if not isinstance(g, dict):
            continue
        status = str(g.get("status") or "?").upper()
        mark = "x" if status == "BLOCKED" else "."
        metrics = g.get("metrics") or {}
        mstr = " " + " ".join(f"{k}={v}" for k, v in metrics.items()) if metrics else ""
        rc = f" reason={g.get('reason_code')}" if g.get("reason_code") else ""
        L.append(f"    [{mark}] {g.get('gate_id')} ({g.get('gate_group')}) {status}{rc}{mstr}")
    return "\n".join(L)


__all__ = ["analyze_traces", "render_markdown", "render_decision_card", "verify_entry_label"]
