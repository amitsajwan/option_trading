"""Config loader — reads ops/strategy_config.yml and projects it into env vars.

The whole point: LIVE (main.py) and SIM (ops_routes.py) call the SAME functions
on the SAME file, so they can never diverge. Because we push the resolved values
into ``os.environ``, none of the ~400 existing ``os.getenv()`` call sites change.

Precedence:
- ``env_wins``  (phase 1): ``os.environ.setdefault`` — existing env (.env.compose)
  still authoritative. Used while migrating, so adding the loader is a no-op.
- ``yaml_wins`` (phase 3): ``os.environ[k] = v`` — YAML is the source of truth.

See ``docs/strategy_platform/CONFIG_CONSOLIDATION_PLAN.md``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .registry import REGISTRY, BY_YAML

logger = logging.getLogger(__name__)

# Default config location. Overridable via STRATEGY_CONFIG_PATH so containers /
# tests can point elsewhere. Resolved relative to repo root (three levels up).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "ops" / "strategy_config.yml"


def _config_path(path: Optional[str | Path] = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.getenv("STRATEGY_CONFIG_PATH")
    return Path(env) if env else DEFAULT_CONFIG_PATH


def _walk(tree: dict, dotted: str) -> tuple[bool, Any]:
    """Return (found, value) for a dotted path in a nested dict."""
    cur: Any = tree
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _flatten_keys(tree: dict, prefix: str = "") -> set[str]:
    """All leaf dotted paths present in the YAML (for unknown-key validation)."""
    out: set[str] = set()
    for k, v in tree.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= _flatten_keys(v, path)
        else:
            out.add(path)
    return out


def load_yaml(path: Optional[str | Path] = None) -> dict:
    """Parse the YAML file. Returns {} if missing (loader then yields all defaults).

    Uses PyYAML when available; otherwise a built-in parser for our simple subset
    (nested 2-space maps, scalar values, block lists). This keeps the loader
    self-contained so it works in any container without a pyyaml dependency.
    """
    p = _config_path(path)
    if not p.exists():
        logger.warning("strategy_config.yml not found at %s — using registry defaults only", p)
        return {}
    text = p.read_text(encoding="utf-8")
    try:
        import yaml  # preferred when present
        data = yaml.safe_load(text) or {}
    except ImportError:
        data = _parse_simple_yaml(text)
    if not isinstance(data, dict):
        logger.error("strategy_config.yml is not a mapping — ignoring")
        return {}
    return data


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML reader for the strategy_config.yml subset (no pyyaml needed).

    Supports: full-line ``#`` comments, blank lines, 2-space nested mappings,
    ``key: value`` scalars, ``key:`` parents, and block sequences (``- item``)
    indented at the key's level. Scalars/lists are returned as strings/list-of-
    strings; the registry coerces final types, so this matches the pyyaml path.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    last_empty: Optional[tuple[dict, str]] = None  # (parent_dict, key) for a pending list

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()

        if stripped.startswith("- "):
            if last_empty is None:
                continue
            d, k = last_empty
            if not isinstance(d.get(k), list):
                d[k] = []
            d[k].append(stripped[2:].strip().strip("'\""))
            continue

        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]

        key, _, val = stripped.partition(":")
        key, val = key.strip(), val.strip()
        if val == "":
            new: dict = {}
            parent[key] = new
            stack.append((indent, new))
            last_empty = (parent, key)   # may become a list if "- " items follow
        else:
            parent[key] = val.strip("'\"")
    return root


def resolve(tree: Optional[dict] = None, *, path: Optional[str | Path] = None) -> dict[str, str]:
    """Build the env-var dict from YAML + registry defaults.

    For every registry key: use the YAML value if present, else the registry
    default. Returns ``{ENV_VAR: str_value}`` ready for os.environ.
    """
    if tree is None:
        tree = load_yaml(path)

    # Warn on YAML keys that no registry entry claims (typo guard).
    unknown = _flatten_keys(tree) - set(BY_YAML.keys())
    for u in sorted(unknown):
        logger.warning("strategy_config.yml: unknown key '%s' (no registry entry)", u)

    out: dict[str, str] = {}
    for key in REGISTRY:
        found, value = _walk(tree, key.yaml_path)
        if not found:
            value = key.default
        out[key.env_var] = key.format(value)
    return out


def apply_to_environ(
    config: Optional[dict[str, str]] = None,
    *,
    precedence: str = "env_wins",
    path: Optional[str | Path] = None,
) -> dict[str, str]:
    """Push resolved config into ``os.environ`` and return what was resolved.

    ``env_wins``  -> setdefault (existing env stays authoritative; phase 1 no-op)
    ``yaml_wins`` -> overwrite (YAML is source of truth; phase 3)
    """
    if config is None:
        config = resolve(path=path)

    if precedence not in ("env_wins", "yaml_wins"):
        raise ValueError(f"precedence must be env_wins|yaml_wins, got {precedence!r}")

    applied = 0
    overrides = 0
    for env_var, value in config.items():
        if value == "":
            # Don't clobber/define empty optionals; let code defaults handle them.
            if precedence == "env_wins":
                continue
        if precedence == "env_wins":
            if env_var not in os.environ:
                os.environ[env_var] = value
                applied += 1
        else:  # yaml_wins
            prior = os.environ.get(env_var)
            if prior is not None and not _same_value(prior, value):
                # Audit the cutover: log only REAL changes (numeric-equal repr
                # differences like "0.10" vs "0.1" are not changes).
                logger.info("strategy_config override: %s env=%r -> yaml=%r",
                            env_var, prior, value)
                overrides += 1
            os.environ[env_var] = value
            applied += 1

    logger.info(
        "strategy_config applied: %d/%d keys (precedence=%s, real_overrides=%d)",
        applied, len(config), precedence, overrides,
    )
    return config


def _same_value(a: str, b: str) -> bool:
    """True if two env strings are equal as values (numeric-aware)."""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False
