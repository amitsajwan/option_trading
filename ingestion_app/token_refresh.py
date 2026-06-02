"""Kite access token auto-refresh — run at 08:30 IST before market open.

Reads KITE_API_KEY and KITE_REQUEST_TOKEN, exchanges for access token,
writes the new KITE_ACCESS_TOKEN to the credentials file and optionally to a
Docker-mounted env-file so running containers pick it up on next restart.

Usage (cron on GCP VM, /etc/cron.d/kite-token):
  30 3 * * 1-5 root /opt/option_trading/.venv/bin/python \
      -m ingestion_app.token_refresh >> /opt/option_trading/.run/token_refresh.log 2>&1

(08:30 IST = 03:00 UTC)

Env vars:
  KITE_API_KEY            — Kite API key (static)
  KITE_API_SECRET         — Kite API secret (static, from developer console)
  KITE_REQUEST_TOKEN      — one-time token from login URL (must be refreshed manually
                            or via a login automation script)
  KITE_CREDENTIALS_PATH   — JSON file to write {api_key, access_token}
                            (default /app/secrets/credentials.json)
  TOKEN_ENV_FILE_PATH     — if set, also writes KITE_ACCESS_TOKEN=<token> to this file
                            (useful for docker-compose --env-file override)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

_CREDENTIALS_PATH = Path(
    os.getenv("KITE_CREDENTIALS_PATH", "/app/secrets/credentials.json")
)
_TOKEN_ENV_FILE = os.getenv("TOKEN_ENV_FILE_PATH", "")


def refresh_token() -> str:
    """Exchange request token for access token. Returns new access token."""
    from kiteconnect import KiteConnect

    api_key = os.getenv("KITE_API_KEY", "")
    api_secret = os.getenv("KITE_API_SECRET", "")
    request_token = os.getenv("KITE_REQUEST_TOKEN", "")

    if not api_key or not api_secret or not request_token:
        raise ValueError(
            "KITE_API_KEY, KITE_API_SECRET, and KITE_REQUEST_TOKEN must all be set"
        )

    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token: str = data["access_token"]
    logger.info("token refreshed: user=%s login_time=%s", data.get("user_name"), data.get("login_time"))
    return access_token


def write_credentials(access_token: str) -> None:
    """Write access token to credentials file and optional env file."""
    api_key = os.getenv("KITE_API_KEY", "")

    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    creds = {"api_key": api_key, "access_token": access_token}
    _CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    logger.info("credentials written to %s", _CREDENTIALS_PATH)

    if _TOKEN_ENV_FILE:
        env_path = Path(_TOKEN_ENV_FILE)
        # Preserve existing lines except KITE_ACCESS_TOKEN
        lines: list[str] = []
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("KITE_ACCESS_TOKEN="):
                    lines.append(line)
        lines.append(f"KITE_ACCESS_TOKEN={access_token}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("env file updated: %s", env_path)


def main() -> int:
    logger.info("kite token refresh starting: %s IST", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        token = refresh_token()
        write_credentials(token)
        logger.info("token refresh complete")
        return 0
    except Exception:
        logger.exception("token refresh FAILED")

        # Send alert if configured
        try:
            from execution_app.alerts import alert_halt
            alert_halt(halt_reason="token_refresh_failed", consecutive_losses=0)
        except Exception:
            pass

        return 1


if __name__ == "__main__":
    sys.exit(main())
