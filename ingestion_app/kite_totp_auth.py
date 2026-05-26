"""Headless Kite authentication using Zerodha credentials + TOTP.

Designed for GCP VM / headless server use — no browser required.
Reads credentials from environment variables, generates a fresh access token,
writes ingestion_app/credentials.json, and updates KITE_ACCESS_TOKEN in .env.compose.

ENV VARS (required)
-------------------
KITE_USER_ID        Zerodha login ID (e.g. BV2032)
KITE_PASSWORD       Zerodha login password
KITE_TOTP_SECRET    32-char base32 TOTP secret from Zerodha Authenticator setup
                    (Settings → Security → Setup TOTP authenticator → show secret)
KITE_API_KEY        Kite Connect app API key
KITE_API_SECRET     Kite Connect app API secret

ENV VARS (optional)
-------------------
KITE_CREDENTIALS_PATH   Output path (default: ingestion_app/credentials.json)
ENV_COMPOSE_PATH        Path to .env.compose to update KITE_ACCESS_TOKEN in (default: .env.compose)
KITE_SKIP_ENV_UPDATE    Set to 1 to skip .env.compose update (default: 0)
KITE_SKIP_DOTENV_LOAD   Set to 1 to skip loading .env file (default: 0)

Usage
-----
    python -m ingestion_app.kite_totp_auth          # normal run
    python -m ingestion_app.kite_totp_auth --verify  # verify existing token only
    python -m ingestion_app.kite_totp_auth --dry-run # show TOTP code, do not write

On GCP VM this is invoked by a systemd timer at 08:30 IST daily.
See ops/gcp/install_token_refresh_timer.sh for setup.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from .env_settings import credentials_path_candidates
from .kite_auth import (
    CredentialsValidator,
    _credentials_output_path,
    _load_dotenv_candidates,
    _serialize_data,
)
from .kite_client import create_kite_client

logger = logging.getLogger(__name__)

_KITE_BASE = "https://kite.zerodha.com"
_LOGIN_URL = f"{_KITE_BASE}/api/login"
_TWOFA_URL = f"{_KITE_BASE}/api/twofa"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _env_compose_path() -> Optional[Path]:
    explicit = str(os.environ.get("ENV_COMPOSE_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    candidates = [
        _repo_root() / ".env.compose",
        _repo_root() / ".env",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _update_env_key(path: Path, key: str, value: str) -> bool:
    """Update or append KEY=value in an env file."""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        replacement = f"{key}={value}"
        if pattern.search(text):
            new_text = pattern.sub(replacement, text)
        else:
            new_text = text.rstrip("\n") + f"\n{replacement}\n"
        path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as exc:
        logger.warning("Failed to update %s in %s: %s", key, path, exc)
        return False


def _generate_totp(secret: str) -> str:
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        logger.error("pyotp not installed — run: pip install pyotp")
        raise


def login_headless(
    *,
    user_id: str,
    password: str,
    totp_secret: str,
    api_key: str,
    api_secret: str,
    credentials_path: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], int]:
    """Full headless login: Zerodha login → TOTP → access token → write credentials.json."""

    output_path = _credentials_output_path(credentials_path)
    session = requests.Session()
    session.headers.update({"X-Kite-Version": "3"})

    # Create client early — needed for login_url() in Step 0 and generate_session() in Step 3.
    kite = create_kite_client(api_key=api_key)

    # Step 0: visit kite.trade/connect/login so the session acquires the
    # KiteConnect app context cookie.  Capture the sess_id from the final
    # redirect URL — it's required as an explicit param to /connect/finish.
    sess_id = ""
    try:
        resp0 = session.get(kite.login_url(), timeout=15, allow_redirects=True)
        m = re.search(r"sess_id=([^&]+)", str(getattr(resp0, "url", "") or ""))
        sess_id = m.group(1) if m else ""
        logger.info("Step 0: KiteConnect session established (sess_id=%s…)", sess_id[:8])
    except Exception as exc:
        logger.warning("Step 0: connect-login pre-visit failed (continuing): %s", exc)

    # Step 1: login with user_id + password
    logger.info("Step 1: logging in as %s", user_id)
    try:
        resp = session.post(
            _LOGIN_URL,
            data={"user_id": user_id, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        logger.error("Login POST failed: %s", exc)
        return None, 1

    if body.get("status") != "success":
        logger.error("Login failed: %s", body.get("message") or body)
        return None, 1

    request_id = (body.get("data") or {}).get("request_id")
    if not request_id:
        logger.error("No request_id in login response: %s", body)
        return None, 1
    logger.info("Step 1 OK — request_id received")

    # Step 2: submit TOTP
    totp_code = _generate_totp(totp_secret)
    logger.info("Step 2: submitting TOTP code %s", totp_code)
    try:
        resp = session.post(
            _TWOFA_URL,
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp",
            },
            timeout=15,
            allow_redirects=False,
        )
    except Exception as exc:
        logger.error("TOTP POST failed: %s", exc)
        return None, 1

    # Extract request_token — try three sources in priority order:
    #   1. Location header (old Zerodha behavior: 302 redirect to callback)
    #   2. /connect/finish endpoint (new Zerodha behavior: 200 JSON, then finish)
    #   3. JSON body at data.request_token (some API versions)
    request_token: Optional[str] = None

    location = resp.headers.get("Location") or ""
    match = re.search(r"request_token=([A-Za-z0-9]+)", location)
    if match:
        request_token = match.group(1)
        logger.info("Step 2 OK — request_token from redirect Location header")

    if not request_token and resp.status_code == 200:
        # New Zerodha behavior: twofa returns 200 JSON; browser then GETs
        # /connect/finish?api_key=...&sess_id=... which issues a 302 to the
        # registered callback URL with request_token.
        # We must NOT follow that redirect — the callback host (127.0.0.1:5000)
        # doesn't exist on the VM.  Read request_token from Location directly.
        logger.info("Step 2: twofa returned 200 — calling /connect/finish (sess_id=%s…)", sess_id[:8])
        try:
            finish_resp = session.get(
                f"{_KITE_BASE}/connect/finish",
                params={"api_key": api_key, "sess_id": sess_id},
                timeout=15,
                allow_redirects=False,
            )
            finish_location = finish_resp.headers.get("Location") or ""
            logger.info("Step 2: /connect/finish → status=%s Location=%s",
                        finish_resp.status_code, finish_location[:120])
            match = re.search(r"request_token=([A-Za-z0-9]+)", finish_location)
            if match:
                request_token = match.group(1)
                logger.info("Step 2 OK — request_token from /connect/finish Location header")
            else:
                match = re.search(r"request_token=([A-Za-z0-9]+)", finish_resp.text[:1000])
                if match:
                    request_token = match.group(1)
                    logger.info("Step 2 OK — request_token from /connect/finish body")
        except Exception as exc:
            logger.error("/connect/finish call failed: %s", exc)

    if not request_token:
        try:
            body2 = resp.json()
            request_token = (body2.get("data") or {}).get("request_token")
            if request_token:
                logger.info("Step 2 OK — request_token from JSON body")
        except Exception:
            pass

    if not request_token:
        logger.error(
            "Could not extract request_token. Status: %s Location: %s Body: %s",
            resp.status_code,
            location[:200],
            resp.text[:300],
        )
        return None, 1

    # Step 3: exchange request_token for access_token via KiteConnect SDK
    logger.info("Step 3: exchanging request_token for access_token")
    try:
        data: dict[str, Any] = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        logger.error("generate_session failed: %s", exc)
        return None, 1

    kite.set_access_token(data["access_token"])
    data = _serialize_data(data)

    cred = {
        "api_key": api_key,
        "api_secret": api_secret,
        "access_token": data.get("access_token"),
        "user_id": data.get("user_id"),
        "data": data,
    }

    # Write credentials.json
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        backup = output_path.with_suffix(output_path.suffix + ".backup")
        backup.write_text(output_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    output_path.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    logger.info("credentials.json written to %s", output_path)

    # Verify
    validator = CredentialsValidator()
    if not validator.verify_credentials(api_key, str(cred.get("access_token") or "")):
        logger.warning("Token written but verification failed")
        return cred, 1

    logger.info("Token verified for user %s", cred.get("user_id"))
    return cred, 0


def update_env_compose(access_token: str) -> None:
    """Write KITE_ACCESS_TOKEN into .env.compose (or .env) so compose picks it up."""
    if str(os.environ.get("KITE_SKIP_ENV_UPDATE") or "0").strip() == "1":
        return
    env_path = _env_compose_path()
    if env_path is None:
        logger.warning("No .env.compose or .env found — skipping env update")
        return
    if _update_env_key(env_path, "KITE_ACCESS_TOKEN", access_token):
        logger.info("KITE_ACCESS_TOKEN updated in %s", env_path)
    else:
        logger.warning("Failed to update KITE_ACCESS_TOKEN in %s", env_path)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Headless Kite TOTP authentication for GCP VM.")
    parser.add_argument("--verify", action="store_true", help="Verify existing token and exit (no new login)")
    parser.add_argument("--dry-run", action="store_true", help="Print current TOTP code without performing login")
    parser.add_argument("--credentials-path", default=None, help="Output path for credentials.json")
    args = parser.parse_args(argv)

    _load_dotenv_candidates()

    api_key = str(os.environ.get("KITE_API_KEY") or "").strip()
    api_secret = str(os.environ.get("KITE_API_SECRET") or "").strip()
    user_id = str(os.environ.get("KITE_USER_ID") or "").strip()
    password = str(os.environ.get("KITE_PASSWORD") or "").strip()
    totp_secret = str(os.environ.get("KITE_TOTP_SECRET") or "").strip()

    if args.dry_run:
        if not totp_secret:
            print("Error: KITE_TOTP_SECRET not set", file=sys.stderr)
            return 1
        code = _generate_totp(totp_secret)
        print(f"Current TOTP code: {code}")
        print(f"Valid for ~{30 - (int(time.time()) % 30)} more seconds")
        return 0

    if args.verify:
        cred_path = _credentials_output_path(args.credentials_path)
        if not cred_path.exists():
            print("No credentials.json found", file=sys.stderr)
            return 1
        cred = json.loads(cred_path.read_text(encoding="utf-8-sig"))
        validator = CredentialsValidator()
        if validator.verify_credentials(api_key, str(cred.get("access_token") or "")):
            print("Token valid")
            return 0
        print("Token invalid or expired")
        return 1

    # Full login
    missing = [k for k, v in [
        ("KITE_API_KEY", api_key),
        ("KITE_API_SECRET", api_secret),
        ("KITE_USER_ID", user_id),
        ("KITE_PASSWORD", password),
        ("KITE_TOTP_SECRET", totp_secret),
    ] if not v]
    if missing:
        print(f"Error: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env or export before running.", file=sys.stderr)
        return 1

    cred, code = login_headless(
        user_id=user_id,
        password=password,
        totp_secret=totp_secret,
        api_key=api_key,
        api_secret=api_secret,
        credentials_path=args.credentials_path,
    )
    if code == 0 and cred:
        update_env_compose(str(cred.get("access_token") or ""))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
