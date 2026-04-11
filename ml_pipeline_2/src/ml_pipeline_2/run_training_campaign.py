from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .campaign.runner import CampaignRunner, resolve_campaign_root
from .campaign.spec import load_campaign_spec


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and run a snapshot training campaign.")
    parser.add_argument("--spec", required=True, help="Path to a campaign JSON spec")
    parser.add_argument("--output-root", help="Optional parent directory for the campaign run root")
    parser.add_argument("--generate-only", action="store_true", help="Generate campaign artifacts without running the factory")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    spec = load_campaign_spec(Path(args.spec))
    campaign_root = resolve_campaign_root(spec, (Path(args.output_root).resolve() if args.output_root else None))
    payload = CampaignRunner(spec, campaign_root).run(generate_only=bool(args.generate_only))
    print(json.dumps(payload, indent=2, default=str))
    if args.generate_only:
        return 0
    factory_result = dict(payload.get("factory_result") or {})
    return 0 if factory_result.get("status") == "publishable_found" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
