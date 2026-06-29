"""Dhan sandbox execution smoke test.

Run inside the runtime VM/container with Dhan credentials available. The command
forces the Dhan sandbox base URL unless DHAN_API_BASE is already set, and refuses
to run unless the effective base points at sandbox.dhan.co.

Example:
  STRATEGY_INSTRUMENT=NIFTY python ops/gcp/dhan_sandbox_smoke.py \
    --expiry 2026-07-02 --strike 24000 --direction CE
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from typing import Any


SANDBOX_BASE = "https://sandbox.dhan.co/v2"


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _json_default(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expiry", required=True, type=_parse_date, help="Option expiry date, YYYY-MM-DD")
    parser.add_argument("--strike", required=True, type=int, help="Option strike")
    parser.add_argument("--direction", required=True, choices=("CE", "PE"), help="Option side to buy")
    parser.add_argument("--lots", type=int, default=1, help="Lots to place; default: 1")
    parser.add_argument("--tag", default="dhan-sandbox-smoke", help="Correlation id prefix")
    parser.add_argument("--status-delay-sec", type=float, default=1.0, help="Seconds to wait before status poll")
    parser.add_argument("--skip-cancel", action="store_true", help="Do not attempt cancellation after status poll")
    args = parser.parse_args()

    if args.lots < 1:
        parser.error("--lots must be >= 1")

    os.environ.setdefault("DHAN_API_BASE", SANDBOX_BASE)
    base = os.getenv("DHAN_API_BASE", "").rstrip("/")
    if base != SANDBOX_BASE:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "refusing to run outside Dhan sandbox",
                    "effective_dhan_api_base": base,
                    "required_dhan_api_base": SANDBOX_BASE,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    # Import after setting DHAN_API_BASE/STRATEGY_INSTRUMENT so module-level
    # instrument selection and adapter config see the intended runtime scope.
    from execution_app.adapter.dhan import DhanAdapter  # noqa: WPS433

    adapter = DhanAdapter()

    result: dict[str, Any] = {
        "ok": False,
        "instrument": os.getenv("STRATEGY_INSTRUMENT") or "BANKNIFTY",
        "dhan_api_base": base,
        "expiry": args.expiry,
        "strike": args.strike,
        "direction": args.direction,
        "lots": args.lots,
    }

    try:
        security_id, quantity = adapter._resolve_qty(args.expiry, args.strike, args.direction, args.lots)
    except ValueError as exc:
        result["error"] = str(exc)
        print(json.dumps(result, indent=2, default=_json_default))
        return 1

    result["security_id"] = security_id
    result["quantity"] = quantity

    order = adapter._place(
        security_id=security_id,
        qty=quantity,
        side="BUY",
        tag=f"{args.tag}-{int(time.time())}",
    )
    result["place"] = order.__dict__

    if order.order_id:
        if args.status_delay_sec > 0:
            time.sleep(args.status_delay_sec)
        status = adapter.get_order_status(order.order_id)
        result["status"] = status.__dict__
        if not args.skip_cancel and status.status not in {"filled", "cancelled", "rejected"}:
            result["cancel_attempted"] = True
            result["cancelled"] = adapter.cancel_order(order.order_id)

    result["ok"] = order.status in {"placed", "filled"} or bool(order.error)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
