"""SIM == LIVE config parity guard.

Enforces "SIM should be same as Live by default" (2026-06-22 directive). The
``strategy_app_sim`` service must expose every decision-affecting env var that
``strategy_app`` does, with the same resolved default — otherwise SIM silently
diverges from live (the bug we found: 46 vars missing → SIM used code defaults
for strike selection, risk sizing, confidence gate, exit max-loss...).

Only a small allow-list of keys may legitimately differ (SIM isolation: separate
Mongo collections, Redis topics/streams, run dir, the per-run id, and the live
market-session scheduler which SIM must NOT run).

This is a pure-text parse of the compose files (no docker needed) so it runs in CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BASE = _REPO / "docker-compose.yml"
_GCP = _REPO / "docker-compose.gcp.yml"

# Keys allowed to differ between live and sim (isolation, not divergence).
_ALLOWED_DIFF_SUBSTR = (
    "MONGO_COLL", "TOPIC", "STREAM_NAME", "RUN_DIR", "SIM_RUN_ID",
    "CONSUMER", "ROLLOUT_STAGE", "MARKET_SESSION_ENABLED",
    "STRATEGY_APP_SIM_IMAGE",
)


def _env_block(path: Path, svc: str) -> dict[str, str]:
    """Extract a service's `environment:` block as {KEY: raw_value_line}."""
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    out: dict[str, str] = {}
    i, n = 0, len(lines)
    while i < n and lines[i].rstrip() != f"  {svc}:":
        i += 1
    if i >= n:
        return {}
    # find environment: within this service (stop if we hit the next top-level service)
    while i < n and not re.match(r"^    environment:", lines[i]):
        if re.match(r"^  \w", lines[i]) and lines[i].rstrip() != f"  {svc}:":
            return {}
        i += 1
    i += 1
    while i < n:
        m = re.match(r"^      ([A-Z_][A-Z0-9_]*):\s?(.*)$", lines[i])
        if m:
            out[m.group(1)] = m.group(2).strip()
        elif re.match(r"^    \w", lines[i]) or re.match(r"^  \w", lines[i]):
            break
        i += 1
    return out


def _merged_env(svc: str) -> dict[str, str]:
    env = _env_block(_BASE, svc)
    env.update(_env_block(_GCP, svc))  # gcp overrides base
    return env


def _allowed(key: str) -> bool:
    return any(s in key for s in _ALLOWED_DIFF_SUBSTR)


def test_sim_has_every_live_decision_key():
    live = _merged_env("strategy_app")
    sim = _merged_env("strategy_app_sim")
    assert live, "could not parse strategy_app environment block"
    assert sim, "could not parse strategy_app_sim environment block"
    missing = sorted(k for k in live if k not in sim and not _allowed(k))
    assert not missing, (
        "strategy_app_sim is MISSING decision env vars that strategy_app has "
        f"(SIM != live): {missing}"
    )


def test_sim_live_shared_keys_have_same_default():
    live = _merged_env("strategy_app")
    sim = _merged_env("strategy_app_sim")
    mismatches = []
    for k, lv in live.items():
        if _allowed(k) or k not in sim:
            continue
        if sim[k] != lv:
            mismatches.append((k, lv, sim[k]))
    assert not mismatches, (
        "strategy_app_sim has DIFFERENT default than strategy_app for "
        f"(SIM != live): {mismatches}"
    )
