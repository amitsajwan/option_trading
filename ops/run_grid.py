"""
Grid experiment runner — ops/run_grid.py

Reads ops/grid.yml and submits one SIM per cell × date via the OPS API.
Prints a P&L comparison table at the end.

Usage:
    python ops/run_grid.py                          # default VM from env or 34.x.x.x
    python ops/run_grid.py --vm-ip 34.1.2.3
    python ops/run_grid.py --dry-run                # print plan, no API calls
    python ops/run_grid.py --dates 2026-06-12       # override dates in grid.yml
    python ops/run_grid.py --cell live_baseline     # run only one cell
    python ops/run_grid.py --port 8008              # dashboard port (default 8008)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import urllib.request
import urllib.error

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)

GRID_YML = Path(__file__).parent / "grid.yml"
DEFAULT_PORT = 8008
POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 120


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


# ── SIM helpers ───────────────────────────────────────────────────────────────

def submit_sim(base_url: str, date: str, overrides: dict) -> str:
    resp = _post(f"{base_url}/api/ops/sim/today", {"date": date, "overrides": overrides})
    return resp["job_id"]


def poll_sim(base_url: str, job_id: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            resp = _get(f"{base_url}/api/ops/sim/{job_id}")
            status = resp.get("status", "")
            if status in ("done", "error", "failed"):
                return resp
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SEC)
    return {"status": "timeout", "job_id": job_id}


def extract_result(resp: dict) -> dict:
    """Pull the key metrics out of a completed SIM response."""
    actual = resp.get("actual_trades", [])
    sim_trades = resp.get("trades", [])

    actual_pnl = [t.get("pnl_pct", 0.0) for t in actual]
    sim_pnl = [t.get("pnl_pct", 0.0) for t in sim_trades]

    def stats(pnls):
        if not pnls:
            return {"n": 0, "avg": 0.0, "total": 0.0, "win_pct": 0.0}
        wins = sum(1 for p in pnls if p > 0)
        return {
            "n": len(pnls),
            "avg": round(sum(pnls) / len(pnls), 4),
            "total": round(sum(pnls), 4),
            "win_pct": round(wins / len(pnls) * 100, 1),
        }

    return {
        "status": resp.get("status"),
        "actual": stats(actual_pnl),
        "sim": stats(sim_pnl),
        "error": resp.get("error", ""),
    }


# ── Table rendering ───────────────────────────────────────────────────────────

def print_table(rows: list[dict]):
    if not rows:
        print("No results.")
        return

    headers = ["date", "cell", "act_n", "act_avg%", "sim_n", "sim_avg%", "status"]
    col_w = {h: max(len(h), max(len(str(r.get(h, ""))) for r in rows)) for h in headers}

    def fmt(row):
        return "  ".join(str(row.get(h, "")).ljust(col_w[h]) for h in headers)

    sep = "  ".join("-" * col_w[h] for h in headers)
    print("\n" + sep)
    print(fmt({h: h for h in headers}))
    print(sep)

    prev_date = None
    for r in rows:
        if prev_date and r["date"] != prev_date:
            print()
        print(fmt(r))
        prev_date = r["date"]
    print(sep)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Grid SIM runner")
    parser.add_argument("--vm-ip", default=os.getenv("RUNTIME_VM_IP", "localhost"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dates", nargs="*", help="Override dates from grid.yml")
    parser.add_argument("--cell", help="Run only this cell (by name)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--grid", default=str(GRID_YML), help="Path to grid.yml")
    args = parser.parse_args()

    grid = yaml.safe_load(Path(args.grid).read_text())
    dates = args.dates or grid.get("dates", [])
    cells = grid.get("cells", [])

    if args.cell:
        cells = [c for c in cells if c["name"] == args.cell]
        if not cells:
            print(f"ERROR: cell '{args.cell}' not found in grid.yml")
            sys.exit(1)

    base_url = f"http://{args.vm_ip}:{args.port}"

    print(f"Grid: {len(cells)} cells × {len(dates)} dates = {len(cells)*len(dates)} SIM runs")
    print(f"API:  {base_url}")

    if args.dry_run:
        print("\n[dry-run] Would submit:")
        for date in dates:
            for cell in cells:
                print(f"  {date}  {cell['name']:30s}  overrides={cell.get('overrides', {})}")
        return

    table_rows = []
    job_map = {}

    # Submit all jobs
    for date in dates:
        for cell in cells:
            name = cell["name"]
            overrides = cell.get("overrides", {})
            try:
                job_id = submit_sim(base_url, date, overrides)
                job_map[(date, name)] = job_id
                print(f"  submitted  {date}  {name}  → {job_id[:8]}...")
            except Exception as e:
                print(f"  ERROR      {date}  {name}  → {e}")
                job_map[(date, name)] = None

    print(f"\nPolling {len(job_map)} jobs (timeout {POLL_TIMEOUT_SEC}s each)...")

    # Poll and collect
    for (date, name), job_id in job_map.items():
        if job_id is None:
            result = {"status": "submit_error", "actual": {}, "sim": {}, "error": "submit failed"}
        else:
            resp = poll_sim(base_url, job_id)
            result = extract_result(resp)

        act = result.get("actual", {})
        sim = result.get("sim", {})
        table_rows.append({
            "date": date,
            "cell": name,
            "act_n": act.get("n", 0),
            "act_avg%": f"{act.get('avg', 0)*100:.2f}",
            "sim_n": sim.get("n", 0),
            "sim_avg%": f"{sim.get('avg', 0)*100:.2f}",
            "status": result.get("status", "?")[:8],
        })

    print_table(table_rows)

    # Also dump raw JSON
    out_path = Path("c:/tmp/grid_result.json")
    out_path.write_text(json.dumps(table_rows, indent=2))
    print(f"\nFull results → {out_path}")


if __name__ == "__main__":
    main()
