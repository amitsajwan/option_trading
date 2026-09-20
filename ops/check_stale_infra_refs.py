#!/usr/bin/env python3
"""Stale infra reference checker -- flags retired GCP project/VM identifiers
still hardcoded as live values in config or scripts.

Born 2026-09-16, same week as check_safety_invariants.py and
check_doc_links.py: a manual grep for this repo's known-dead identifiers
(after 3 undocumented GCP project migrations: amit-trading ->
amittrading-493606 -> algo-trading-496203 -> trader-502012) found REAL,
currently-hazardous bugs, not just stale prose -- infra/gcp/terraform.tfvars
still targeted a dead project (a real `terraform apply` would have hit the
wrong project), ops/gcp/backup_to_local.sh (the actual disaster-recovery
backup script) hardcoded a dead project, and ops/gcp/patch_operator_env.py
would have actively broken a fresh checkout if run. This script makes that
grep a standing, maintainable check instead of a one-off sweep.

Design notes:
  - Only checks .py/.sh/.yml/.yaml/.tfvars/.env* files -- prose docs (.md)
    legitimately reference old identifiers in "here's what changed"
    historical narration; that's not a bug the way a hardcoded config
    value is. check_doc_links.py covers docs separately.
  - Skips full-line comments (line's first non-whitespace char is '#').
    Explaining history in a comment is exactly the right way to retire an
    identifier -- see every fix commit this same week for examples. This
    check is for identifiers still live in an operative line: an
    assignment, a URL, a CLI flag.
  - Skips docs/archive/ and untracked files (git ls-files is the source of
    truth) -- frozen history and personal local overrides are out of scope.
  - RETIRED list is a plain list to extend by hand on every future
    migration. When trader-502012 is eventually retired, add it here (with
    its replacement) rather than deleting the old entries -- old mistakes
    tend to get copy-pasted back in from an even-older branch or backup.

Exit code 0 = no retired identifier appears in an operative (non-comment)
line of any in-scope file. Exit code 1 = at least one does.

Usage:
    python3 ops/check_stale_infra_refs.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_EXTENSIONS = (".py", ".sh", ".yml", ".yaml", ".tfvars")
# .env-style files don't all share one extension (.env, .env.example, ...);
# matched separately below by filename pattern.

RETIRED = [
    {"value": "amit-trading", "kind": "GCP project ID", "replaced_by": "trader-502012"},
    {"value": "amittrading-493606", "kind": "GCP project ID", "replaced_by": "trader-502012"},
    {"value": "algo-trading-496203", "kind": "GCP project ID", "replaced_by": "trader-502012"},
    {"value": "option-trading-runtime-01", "kind": "runtime VM name", "replaced_by": "trader-runtime-01"},
    {"value": "option-trading-ml-01", "kind": "training VM name", "replaced_by": "trader-training-01 (or any current unique name)"},
]

# Files that are allowed to contain a retired identifier in an operative
# line, with why. Keep this short; prefer fixing the file over adding to it.
ALLOWLIST_PATHS: dict[str, str] = {
    "ops/deploy_compression_v1.sh": (
        "tied to the unmerged feat/compression-state-engine branch; whether "
        "that branch is still alive is a pending human decision, not a "
        "doc/config staleness issue -- see project_docs_consolidation_2026-09-16 "
        "in memory"
    ),
    "ops/check_stale_infra_refs.py": (
        "this file's own RETIRED registry necessarily contains these exact "
        "strings as data (that's the whole point) -- it isn't live usage. "
        "Didn't self-flag the first time this script was tested because it "
        "wasn't yet git-tracked at that point; caught on the next run after "
        "it was committed."
    ),
}


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True,
    )
    paths = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        p = REPO_ROOT / line
        if "docs/archive/" in line.replace("\\", "/"):
            continue
        name = p.name
        is_env_file = name == ".env" or name.startswith(".env.") or name.endswith(".env")
        if p.suffix in SCAN_EXTENSIONS or is_env_file:
            paths.append(p)
    return paths


def check_file(path: Path) -> list[str]:
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    if rel in ALLOWLIST_PATHS:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return []

    is_python = path.suffix == ".py"
    in_docstring = False
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()

        if is_python:
            # Naive but sufficient for this repo's style: toggle on each
            # line containing a triple-quote, so prose inside a module/
            # function docstring isn't treated as an operative code line.
            # Does not handle a docstring opening and closing on the same
            # line differently from opening OR closing -- both flip state
            # once per occurrence, which is what actually matters here.
            quote_count = stripped.count('"""') + stripped.count("'''")
            was_in_docstring = in_docstring
            if quote_count % 2 == 1:
                in_docstring = not in_docstring
            if was_in_docstring or (in_docstring and quote_count):
                continue

        if not stripped or stripped.startswith("#"):
            continue
        for entry in RETIRED:
            if entry["value"] in line:
                problems.append(
                    f"{rel}:{lineno}: retired {entry['kind']} '{entry['value']}' "
                    f"still live -- replace with '{entry['replaced_by']}': {stripped[:100]}"
                )
    return problems


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    files = _tracked_files()
    all_problems: list[str] = []
    for f in files:
        all_problems.extend(check_file(f))

    if not all_problems:
        print(f"[check_stale_infra_refs] OK -- scanned {len(files)} config/script files, no retired identifiers found live.")
        return 0

    print(f"[check_stale_infra_refs] {len(all_problems)} retired identifier(s) still live:\n")
    for p in all_problems:
        print(f"  {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
