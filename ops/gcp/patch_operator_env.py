#!/usr/bin/env python3
"""
patch_operator_env.py

Replaces placeholder values in ops/gcp/operator.env with real project values
for the amittrading-493606 GCP project.

Run once after cloning on a new machine or after VM rebuild:

    python3 ops/gcp/patch_operator_env.py [--repo-root /path/to/repo]

Safe to re-run — only replaces known placeholder strings, leaves other values untouched.
"""
from __future__ import annotations
import argparse
import pathlib
import sys


REPLACEMENTS = [
    # GCP project
    ('my-gcp-project',              'amittrading-493606'),
    # Zone (terraform.tfvars uses asia-south1-b, not -a)
    ('ZONE="asia-south1-a"',        'ZONE="asia-south1-b"'),
    # Repo
    ('your-github-org',             'amitsajwan'),
    # Runtime VM name
    ('RUNTIME_NAME="option-trading-runtime"', 'RUNTIME_NAME="option-trading-runtime-01"'),
    # Buckets
    ('my-option-trading-models',              'amittrading-493606-option-trading-models'),
    ('my-option-trading-runtime-config',      'amittrading-493606-option-trading-runtime-config'),
    # Data sync
    ('my-training-data-root/ml_pipeline',
     'amittrading-493606-option-trading-snapshots/ml_pipeline'),
]


def patch(repo_root: pathlib.Path) -> None:
    env_file = repo_root / 'ops' / 'gcp' / 'operator.env'
    if not env_file.exists():
        print(f'ERROR: {env_file} not found — copy operator.env.example first', file=sys.stderr)
        sys.exit(1)

    text = env_file.read_text(encoding='utf-8')
    changed = 0
    for old, new in REPLACEMENTS:
        if old in text:
            text = text.replace(old, new)
            print(f'  replaced: {old!r}')
            changed += 1
        else:
            print(f'  skip (not found): {old!r}')

    env_file.write_text(text, encoding='utf-8')
    print(f'\nPatched {changed} value(s) in {env_file}')

    # Verify key fields
    print('\nVerification:')
    for key in ['PROJECT_ID', 'ZONE', 'RUNTIME_NAME', 'MODEL_BUCKET_URL', 'RUNTIME_CONFIG_BUCKET_URL']:
        for line in text.splitlines():
            if line.startswith(f'{key}='):
                print(f'  {line}')
                break


def main() -> None:
    parser = argparse.ArgumentParser(description='Patch operator.env placeholders with real values')
    parser.add_argument('--repo-root', default='.',
                        help='Path to repo root (default: current directory)')
    args = parser.parse_args()
    patch(pathlib.Path(args.repo_root).resolve())


if __name__ == '__main__':
    main()
