"""Config diff tool for comparing staged manifest variations.

Usage:
    from ml_pipeline_2.staged.config_diff import diff_manifests, print_diff
    from ml_pipeline_2.staged.scenario_runner import build_manifest

    m1 = build_manifest(bypass_stage2=False)
    m2 = build_manifest(bypass_stage2=True)
    print_diff(diff_manifests(m1, m2))
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple


def diff_manifests(a: Dict[str, Any], b: Dict[str, Any], *, prefix: str = "") -> List[Dict[str, Any]]:
    """Return a list of differences between two manifest dicts."""
    diffs: list[dict[str, Any]] = []
    all_keys = set(a.keys()) | set(b.keys())

    for key in sorted(all_keys):
        path = f"{prefix}.{key}" if prefix else key
        if key not in a:
            diffs.append({"path": path, "type": "added", "value": b[key]})
        elif key not in b:
            diffs.append({"path": path, "type": "removed", "value": a[key]})
        elif isinstance(a[key], dict) and isinstance(b[key], dict):
            diffs.extend(diff_manifests(a[key], b[key], prefix=path))
        elif a[key] != b[key]:
            diffs.append({
                "path": path,
                "type": "changed",
                "old": a[key],
                "new": b[key],
            })

    return diffs


def print_diff(diffs: Sequence[Dict[str, Any]], *, max_value_len: int = 80) -> None:
    """Pretty-print manifest differences."""
    if not diffs:
        print("No differences found.")
        return

    for d in diffs:
        path = d["path"]
        if d["type"] == "added":
            val = _trunc(d["value"], max_value_len)
            print(f"  + {path}: {val}")
        elif d["type"] == "removed":
            val = _trunc(d["value"], max_value_len)
            print(f"  - {path}: {val}")
        elif d["type"] == "changed":
            old = _trunc(d["old"], max_value_len)
            new = _trunc(d["new"], max_value_len)
            print(f"  ~ {path}")
            print(f"      old: {old}")
            print(f"      new: {new}")


def _trunc(value: Any, max_len: int) -> str:
    s = repr(value)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


__all__ = ["diff_manifests", "print_diff"]
