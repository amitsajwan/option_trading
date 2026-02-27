"""Compatibility shim.

Canonical MarketSnapshot implementation now lives in:
    market_data.market_snapshot

This module keeps legacy imports and CLI entrypoints stable.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _ensure_market_data_src_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    md_src = repo_root / "market_data" / "src"
    md_src_str = str(md_src)
    if md_src_str in sys.path:
        sys.path.remove(md_src_str)
    sys.path.insert(0, md_src_str)


_ensure_market_data_src_on_path()
_impl_mod = importlib.import_module("market_data.market_snapshot")

for _export_name in dir(_impl_mod):
    if _export_name.startswith("__") and _export_name.endswith("__"):
        continue
    if _export_name in {"_impl", "_impl_mod", "_name", "_export_name"}:
        continue
    globals()[_export_name] = getattr(_impl_mod, _export_name)

run_cli = getattr(_impl_mod, "run_cli")

del _export_name
del _impl_mod


if __name__ == "__main__":
    raise SystemExit(run_cli())
