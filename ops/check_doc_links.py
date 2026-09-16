#!/usr/bin/env python3
"""Doc link checker -- finds markdown links pointing at files that don't exist.

Born 2026-09-16 during a documentation cleanup: at least 4 separate "index"
docs (docs/README.md, docs/SYSTEM_SOURCE_OF_TRUTH.md, docs/strategy_platform/
README.md, strategy_app/docs/README.md) were found linking to files archived
or deleted in earlier cleanup passes, sometimes months earlier, because
nobody re-grepped for referrers when they moved or removed the target. This
script is the mechanical version of that grep, run once instead of by hand.

Only checks relative links to other files in the repo (http(s):// links and
pure same-page anchors are skipped -- those need a network call or DOM
parse respectively, out of scope here). A link with a `#fragment` has the
fragment stripped before the file-existence check; fragment correctness is
not verified.

Exit code 0 = every relative link resolves. Exit code 1 = at least one
doesn't (printed as `file:line: [text](target) -> target does not exist`).

Usage:
    python3 ops/check_doc_links.py                  # scan the whole repo
    python3 ops/check_doc_links.py docs/ strategy_app/docs/   # scan specific dirs
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# [link text](target) -- deliberately does not match reference-style
# [text][ref] links or bare autolinks, which are rare in this repo.
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)\)")

SKIP_DIR_NAMES = {"node_modules", ".venv", ".git", "__pycache__", ".pytest_cache"}


def _tracked_markdown_files() -> list[Path]:
    """Use git ls-files so generated/vendored .md files never get scanned."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.md"],
        capture_output=True, text=True, check=True,
    )
    paths = [REPO_ROOT / line for line in result.stdout.splitlines() if line.strip()]
    return [p for p in paths if not any(part in SKIP_DIR_NAMES for part in p.parts)]


def _is_external_or_special(target: str) -> bool:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return True
    if target.startswith("gs://") or target.startswith("s3://"):
        return True
    return False


def check_file(md_path: Path, all_ok: list[bool]) -> list[str]:
    problems = []
    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return problems
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in LINK_RE.finditer(line):
            target = m.group(1)
            if _is_external_or_special(target):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            resolved = (md_path.parent / target_path).resolve()
            if not resolved.exists():
                rel = md_path.relative_to(REPO_ROOT)
                problems.append(f"{rel}:{lineno}: [{m.group(0)}] -> {target_path} does not exist")
                all_ok.append(False)
    return problems


def main(argv: list[str]) -> int:
    # Windows consoles default to cp1252, which can't encode every character
    # that shows up in link text (arrows, em-dashes, etc.) -- force UTF-8 so
    # this doesn't crash mid-run on an otherwise-successful scan.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if argv:
        files = []
        for arg in argv:
            p = (REPO_ROOT / arg).resolve()
            if p.is_dir():
                files.extend(sorted(p.rglob("*.md")))
            elif p.exists():
                files.append(p)
        files = [f for f in files if not any(part in SKIP_DIR_NAMES for part in f.parts)]
    else:
        files = _tracked_markdown_files()

    all_ok: list[bool] = []
    all_problems: list[str] = []
    for f in files:
        all_problems.extend(check_file(f, all_ok))

    if not all_problems:
        print(f"[check_doc_links] OK -- scanned {len(files)} markdown files, all relative links resolve.")
        return 0

    print(f"[check_doc_links] {len(all_problems)} broken link(s) across {len(files)} scanned files:\n")
    for p in all_problems:
        print(f"  {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
