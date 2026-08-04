"""Instrument-surface checks — the single source of truth for BOTH:

  * tests/test_instrument_hardcoding_lint.py  (CI guardrail)
  * ops/onboard_instrument_check.py           (onboarding preflight CLI)

Born 2026-08-04, after FINNIFTY's onboarding surfaced ~25 code sites that
hardcoded instrument names/lists instead of deriving from
contracts_app.instruments — several were live-money bugs (wrong expiry
cadence in backfill, a no-op lot-size env var, zero monitoring coverage,
dashboard 400s). Every check here turns that class of drift into a test
failure / preflight TODO instead of a live incident.

Two kinds of check:
  1. ast_scan_file(): flags NEW registry-bypassing code (dict/list/tuple
     literals or comparison chains keyed on >=2 instrument names).
  2. surface_checks(instrument): per-instrument completeness across compose
     files, deploy.sh, the config contract, monitoring scripts, and the
     WS/depth instrument maps — everything a new instrument must touch.

Pure stdlib + repo-relative text parsing (no docker, no network) so it runs
identically in CI and on the VM.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from contracts_app.instruments import known_instruments  # noqa: E402
from contracts_app.sim_namespace import PRIMARY_INSTRUMENT  # noqa: E402

# ── AST hardcoding scan ──────────────────────────────────────────────────────

# Files allowed to contain multi-instrument literals, with the reason.
# Everything here is either THE definition site, a deliberate alias table
# with a safe fallback, or a one-shot operator study script (not runtime).
AST_ALLOWLIST: dict[str, str] = {
    "contracts_app/instruments.py": "the registry itself",
    "ml_pipeline_2/scripts/dhan_data_pipeline.py":
        "pipeline's own INSTRUMENTS dict -- kept in sync by test_registry_parity_with_pipeline",
    "snapshot_app/core/live_ml_flat.py":
        "NSE display-name alias table (safe [underlying] fallback)",
    "market_data_dashboard/app.py":
        "NSE display-name aliases for exchange inference (registry consulted too)",
    "ingestion_app/dhan_ws_feed.py":
        "_FUTURES_LABEL_ENV env-name map (skip-not-wrong fallback; completeness-checked below)",
    "ingestion_app/collectors/dhan_depth_collector.py":
        "self-contained by design per its docstring; completeness-checked below",
    "ingestion_app/dhan_client.py": "legacy IDX_* constants (single consumers)",
    "ingestion_app/api_service.py": "symbol-normalization aliases",
    "ml_pipeline_2/scripts/dhan_build_sim_snapshots.py": "operator sim tool",
    # One-shot operator study/debug scripts (category E in the 2026-08-04 sweep):
    "ops/dhan_replay_publish_from_mongo.py": "study script",
    "ops/dhan_replay_publish_historical.py": "study script",
    "ops/dhan_replay_publish_multiday_from_mongo.py": "study script",
    "ops/study_best_possible_exit.py": "study script",
    "ops/study_direction_counterfactual.py": "study script",
    "ops/study_entry_model_only.py": "study script",
    "ops/_debug_iv_percentile_check.py": "debug script",
    "ops/reenrich_hist_ema.py": "one-shot repair script",
    "ops/depth_atm_sync.py": "operator tool",
    "ops/nifty_sim_today.py": "study script",
    "ops/manual_trail.py": "operator tool",
    "strategy_app/sim/exit_replay.py": "replay-era study harness",
    "strategy_app/tools/offline_strategy_analysis.py": "offline study tool",
    "ops/dev/first30_check_claude.py": "one-shot daily-brief dev check script",
}

_EXCLUDED_DIR_PARTS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", "archive", "tests",
    ".run", ".data", ".claude",
}


def _instrument_names() -> set[str]:
    return set(known_instruments())


def iter_scannable_python_files() -> list[Path]:
    out = []
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO)
        parts = set(part.lower() for part in rel.parts)
        if parts & _EXCLUDED_DIR_PARTS:
            continue
        if rel.parts and rel.parts[0] == "docs":
            continue
        if rel.name.startswith("test_") or rel.name.startswith("conftest"):
            continue
        out.append(p)
    return sorted(out)


def _names_in_literal(node: ast.AST, names: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(node, ast.Dict):
        elems = list(node.keys)
    elif isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        elems = list(node.elts)
    else:
        return found
    for e in elems:
        if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value in names:
            found.add(e.value)
    return found


def _in_except_handler(path_stack: list[ast.AST]) -> bool:
    return any(isinstance(a, ast.ExceptHandler) for a in path_stack)


def ast_scan_file(path: Path) -> list[str]:
    """Return violations for one file: multi-instrument literals or
    comparison chains outside except-handler fallbacks."""
    names = _instrument_names()
    rel = path.relative_to(REPO).as_posix()
    if rel in AST_ALLOWLIST:
        return []
    try:
        # utf-8-sig: several repo files carry a BOM (written on Windows).
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError as exc:
        return [f"{rel}: unparseable ({exc})"]

    violations: list[str] = []

    # Walk with an explicit stack so except-handler fallbacks are exempt
    # (a hardcoded fallback in an `except:` block is a deliberate degraded
    # mode for when contracts_app is unimportable, not registry bypass).
    def walk(node: ast.AST, stack: list[ast.AST], func_cmp: dict) -> None:
        if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
            found = _names_in_literal(node, names)
            if len(found) >= 2 and not _in_except_handler(stack):
                violations.append(
                    f"{rel}:{node.lineno}: literal enumerates {sorted(found)} -- "
                    "derive from contracts_app.known_instruments()/get_instrument() instead"
                )
        if isinstance(node, ast.Compare) and not _in_except_handler(stack):
            for cmp_node in [node.left, *node.comparators]:
                if isinstance(cmp_node, ast.Constant) and isinstance(cmp_node.value, str) \
                        and cmp_node.value in names:
                    fn = next((a for a in reversed(stack)
                               if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
                    key = id(fn) if fn is not None else 0
                    func_cmp.setdefault(key, set()).add(cmp_node.value)
                    func_cmp.setdefault(f"{key}_line", node.lineno)
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, stack, func_cmp)
        stack.pop()

    func_cmp: dict = {}
    walk(tree, [], func_cmp)
    for key, found in list(func_cmp.items()):
        if isinstance(key, str):
            continue
        if len(found) >= 2:
            line = func_cmp.get(f"{key}_line", "?")
            violations.append(
                f"{rel}:{line}: comparison chain against {sorted(found)} -- "
                "a >=2-name if/elif chain must be registry-derived"
            )
    return violations


def ast_scan_repo() -> list[str]:
    out: list[str] = []
    for p in iter_scannable_python_files():
        out.extend(ast_scan_file(p))
    return out


# ── Per-instrument surface completeness ─────────────────────────────────────

def _read(rel: str) -> str:
    p = REPO / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def surface_checks(instrument: str) -> list[tuple[str, bool, str]]:
    """(surface, ok, detail) for every place a live instrument must exist.

    For the PRIMARY instrument most suffix checks are trivially true (it is
    the unsuffixed base stack).
    """
    inst = instrument.strip().upper()
    slug = inst.lower()
    primary = inst == PRIMARY_INSTRUMENT
    sfx = "" if primary else f"_{slug}"
    checks: list[tuple[str, bool, str]] = []

    def add(surface: str, ok: bool, detail: str) -> None:
        checks.append((surface, ok, detail))

    # 1. registry
    add("contracts_app registry", inst in known_instruments(),
        "add InstrumentSpec to contracts_app/instruments.py")

    # 2. training-pipeline INSTRUMENTS dict (parity-tested but check presence)
    pipe = _read("ml_pipeline_2/scripts/dhan_data_pipeline.py")
    add("ml_pipeline_2 INSTRUMENTS dict", f'"{inst}": InstrumentConfig(' in pipe,
        "add InstrumentConfig to ml_pipeline_2/scripts/dhan_data_pipeline.py")

    # 3. WS futures env map
    ws = _read("ingestion_app/dhan_ws_feed.py")
    add("dhan_ws_feed _FUTURES_LABEL_ENV", f'"{inst}"' in ws,
        f'add "{inst}": "{inst}_INSTRUMENT_SYMBOL" to _FUTURES_LABEL_ENV')

    # 4. depth collector maps
    depth = _read("ingestion_app/collectors/dhan_depth_collector.py")
    add("dhan_depth_collector maps", f'"{inst}"' in depth,
        "add to _INDEX_SECURITY_ID and _DEFAULT_STRIKE_STEP")

    # 5. compose: gcp.yml service blocks
    gcp = _read("docker-compose.gcp.yml")
    needed_gcp = ([f"depth_collector_dhan{sfx}:"] if primary else [
        f"ingestion_app{sfx}:", f"snapshot_app{sfx}:", f"persistence_app{sfx}:",
        f"strategy_app{sfx}:", f"execution_app{sfx}:",
        f"strategy_persistence_app{sfx}:", f"depth_collector_dhan{sfx}:",
    ])
    missing = [s for s in needed_gcp if f"\n  {s}" not in gcp]
    add("docker-compose.gcp.yml services", not missing,
        f"missing service blocks: {missing}" if missing else "")

    # 6. owner WS symbol env on the base ingestion_app block
    if not primary:
        add("gcp.yml owner FINNIFTY-style symbol env",
            f"{inst}_INSTRUMENT_SYMBOL" in gcp,
            f"add {inst}_INSTRUMENT_SYMBOL to the owner ingestion_app env block")

    # 7. seller overlay
    seller = _read("docker-compose.seller.yml")
    seller_ok = (f"\n  seller_app{sfx}:" in seller) if not primary else ("\n  seller_app:" in seller)
    vol_ok = True if primary else (f"seller_{slug}_run:" in seller)
    add("docker-compose.seller.yml", seller_ok and vol_ok,
        f"add seller_app{sfx} block and seller_{slug}_run volume")

    # 8. base compose depth collector
    base = _read("docker-compose.yml")
    add("docker-compose.yml depth collector", f"depth_collector_dhan{sfx}:" in base,
        f"add depth_collector_dhan{sfx} to docker-compose.yml")

    # 9. deploy.sh CORE_SERVICES
    deploy = _read("ops/deploy.sh")
    m = re.search(r'^CORE_SERVICES="([^"]*)"', deploy, re.M)
    core = set((m.group(1) if m else "").split())
    needed_core = {f"ingestion_app{sfx}", f"snapshot_app{sfx}", f"strategy_app{sfx}",
                   f"execution_app{sfx}", f"seller_app{sfx}"}
    missing_core = sorted(needed_core - core)
    add("deploy.sh CORE_SERVICES", not missing_core,
        f"append: {' '.join(missing_core)}" if missing_core else "")

    # 10. config contract entries
    try:
        contract = json.loads(_read("ops/config_contract_expected.json"))
        services = set(contract.get("services", {}))
    except Exception:
        services = set()
    needed_contract = {f"option_trading-strategy_app{sfx}-1",
                       f"option_trading-execution_app{sfx}-1",
                       f"option_trading-seller_app{sfx}-1"}
    missing_contract = sorted(needed_contract - services)
    add("config_contract_expected.json", not missing_contract,
        f"add service blocks: {missing_contract}" if missing_contract else "")

    # 11. monitoring is generic -- assert the generic loop markers exist so a
    # revert to hardcoded lists fails loudly for EVERY instrument.
    installer = _read("ops/gcp/install_dhan_token_units.sh")
    add("feature-health systemd unit is instrument-generic",
        "strategy_app(_[a-z]+)?-1" in installer,
        "restore the generic docker-ps loop in install_dhan_token_units.sh")
    liveness = _read("ops/gcp/check_strategy_liveness.sh")
    add("liveness probe is instrument-generic",
        "strategy_app(_[a-z]+)?-1" in liveness,
        "restore the generic docker-ps loop in check_strategy_liveness.sh")
    # 2026-08-04: this exact hardcoded-list shape hit REAL MONEY twice
    # (2026-07-24 seller_app_nifty missing from the daily token-refresh list;
    # 2026-08-04 FINNIFTY missing entirely, discovered at market open when
    # ingestion_app_finnifty was still authenticating on yesterday's token).
    # A bash script, so the AST scan above (Python-only) can't catch it.
    token_refresh = _read("ops/gcp/dhan_token_refresh.sh")
    add("dhan token-refresh recreate-list is instrument-generic",
        "(ingestion_app|execution_app|seller_app|depth_collector_dhan)(_[a-z]+)?-1" in token_refresh,
        "restore the generic docker-ps loop in ops/gcp/dhan_token_refresh.sh")

    # 12. lot-size env is actually read (the FINNIFTY_LOT_SIZE no-op class)
    consts = _read("strategy_app/constants.py")
    add("{INST}_LOT_SIZE env is readable generically",
        '_LOT_SIZE"' in consts and 'f"{inst}' in consts,
        "resolve_lot_size() lost its generic {INST}_LOT_SIZE branch")

    return checks


def surface_problems(instrument: str) -> list[str]:
    return [f"[{instrument}] {surface}: {detail or 'MISSING'}"
            for surface, ok, detail in surface_checks(instrument)
            if not ok]
