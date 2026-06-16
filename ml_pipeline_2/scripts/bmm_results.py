"""Summarize the BMM grid as runs complete (log-driven, robust to schema drift).

For each run it reads $HOME/bmm_logs/<run>.log, classifies state
(running / done / error), and — when done — pulls the final summary JSON that
run_research prints and recursively extracts the stage1 ROC-AUC, the half-split
drift, and the publish/gate status. Prints a comparison table vs the v3 baseline
(AUC 0.831).

Usage:
  python ml_pipeline_2/scripts/bmm_results.py            # one-shot table
  python ml_pipeline_2/scripts/bmm_results.py --watch    # refresh every 30s until all done
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

RUNS = [
    ("bmm_h05m_010pct", "5m / 0.10% (~54pt)"),
    ("bmm_h10m_015pct", "10m / 0.15% (~80pt)"),
    ("bmm_h15m_020pct", "15m / 0.20% (~108pt)"),
    ("bmm_h20m_030pct", "20m / 0.30% (~160pt)"),
    ("bmm_h30m_040pct", "30m / 0.40% (~216pt)"),
]
LOGDIR = Path(os.environ.get("BMM_LOGDIR", str(Path.home() / "bmm_logs")))
V3_BASELINE_AUC = 0.831


def _last_json_object(text: str):
    """Return the last top-level {...} JSON object parseable from text, else None."""
    starts = [m.start() for m in re.finditer(r"\n\{", "\n" + text)]
    for s in reversed(starts):
        snippet = text[max(0, s - 1):]
        # find matching brace by scanning
        depth = 0
        for i, ch in enumerate(snippet):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(snippet[: i + 1])
                    except Exception:
                        break
    return None


def _find_first(obj, key):
    """Depth-first search for the first value of `key` in nested dict/list."""
    if isinstance(obj, dict):
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
        for v in obj.values():
            r = _find_first(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first(v, key)
            if r is not None:
                return r
    return None


def _classify(log: Path):
    if not log.exists():
        return "no-log", {}
    text = log.read_text(errors="replace")
    if "Traceback (most recent call last)" in text and not text.rstrip().endswith("}"):
        # error unless a valid final JSON followed
        obj = _last_json_object(text)
        if obj is None:
            tail = text.strip().splitlines()[-1:] or [""]
            return "ERROR", {"msg": tail[-1][:80]}
    obj = _last_json_object(text)
    if obj is None:
        return "running", {}
    auc = _find_first(obj, "roc_auc")
    drift = _find_first(obj, "roc_auc_drift_half_split")
    status = _find_first(obj, "publish_status") or _find_first(obj, "release_status")
    pf = _find_first(obj, "profit_factor")
    return "done", {"auc": auc, "drift": drift, "status": status, "pf": pf}


def _fmt(v, nd=4):
    try:
        return f"{float(v):.{nd}f}"
    except Exception:
        return "-"


def render() -> bool:
    print(f"\n=== BMM grid results (logs: {LOGDIR}) — v3 baseline AUC={V3_BASELINE_AUC} ===")
    print(f"{'run':18} {'horizon/label':22} {'state':8} {'stage1_AUC':10} {'vs_v3':7} {'drift':7} {'PF':6} {'gate'}")
    all_done = True
    for run, label in RUNS:
        state, info = _classify(LOGDIR / f"{run}.log")
        if state != "done":
            all_done = all_done and state in ("ERROR",)
        auc = info.get("auc")
        vs = ""
        if auc is not None:
            try:
                vs = f"{float(auc) - V3_BASELINE_AUC:+.3f}"
            except Exception:
                vs = ""
        print(f"{run:18} {label:22} {state:8} {_fmt(auc):10} {vs:7} {_fmt(info.get('drift'),3):7} {_fmt(info.get('pf'),2):6} {str(info.get('status') or '-')}")
    return all_done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    a = ap.parse_args()
    if not a.watch:
        render()
        return 0
    while True:
        done = render()
        if done:
            print("\nall runs finished.")
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
