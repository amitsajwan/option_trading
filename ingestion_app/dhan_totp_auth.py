"""Headless Dhan authentication using client-id + PIN + TOTP.

Designed for GCP VM / headless server use — no browser required. Mirrors the
Kite TOTP flow (ingestion_app/kite_totp_auth.py) but Dhan's auth is a single
POST, not a multi-step browser-emulated login.

Reads credentials from environment variables, generates a fresh access token via
`POST https://auth.dhan.co/app/generateAccessToken`, writes
ingestion_app/dhan_credentials.json, and updates DHAN_ACCESS_TOKEN in .env.compose.

ENV VARS (required)
-------------------
DHAN_CLIENT_ID      Dhan client id (numeric, e.g. 11119xxxxx)
DHAN_PIN            6-digit Dhan PIN
DHAN_TOTP_SECRET    base32 TOTP secret from the Dhan 2FA / Authenticator setup

ENV VARS (optional)
-------------------
DHAN_CREDENTIALS_PATH   Output path (default: ingestion_app/dhan_credentials.json)
ENV_COMPOSE_PATH        Path to .env.compose to update DHAN_ACCESS_TOKEN in (default: .env.compose)
DHAN_SKIP_ENV_UPDATE    Set to 1 to skip .env.compose update (default: 0)
KITE_SKIP_DOTENV_LOAD   Set to 1 to skip loading .env file (default: 0)

Usage
-----
    python -m ingestion_app.dhan_totp_auth            # normal run (generate token)
    python -m ingestion_app.dhan_totp_auth --verify   # verify existing token only
    python -m ingestion_app.dhan_totp_auth --dry-run  # show TOTP code, no network

SECURITY: never logs/prints the PIN or the TOTP secret. The transient 30-second
TOTP code is printed only under --dry-run (explicit debug). Secrets live in
/opt/option_trading/.kite_secrets on the VM, never in the repo.

On the GCP VM this is invoked by a systemd timer ~08:30 IST daily (same pattern
as the Kite timer). The endpoint is documented but UNVERIFIED against a live
account — the first real run is a one-shot verification (see docs/.../DHAN_API_MIGRATION.md §2.1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .kite_auth import _load_dotenv_candidates

logger = logging.getLogger(__name__)

_DHAN_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
_DHAN_PROFILE_URL = "https://api.dhan.co/v2/profile"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _credentials_output_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env_path = str(os.environ.get("DHAN_CREDENTIALS_PATH") or "").strip()
    if env_path:
        return Path(env_path).resolve()
    return _repo_root() / "ingestion_app" / "dhan_credentials.json"


def _env_compose_path() -> Optional[Path]:
    explicit = str(os.environ.get("ENV_COMPOSE_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    for p in (_repo_root() / ".env.compose", _repo_root() / ".env"):
        if p.exists():
            return p
    return None


def _update_env_key(path: Path, key: str, value: str) -> bool:
    """Update or append KEY=value in an env file."""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        replacement = f"{key}={value}"
        new_text = pattern.sub(replacement, text) if pattern.search(text) else text.rstrip("\n") + f"\n{replacement}\n"
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


def _extract_token(resp_json: Any) -> Optional[str]:
    """Pull the access token out of Dhan's response, tolerant of key naming."""
    if not isinstance(resp_json, dict):
        return None
    for container in (resp_json, resp_json.get("data") or {}):
        if not isinstance(container, dict):
            continue
        for key in ("accessToken", "access_token", "token"):
            val = container.get(key)
            if val:
                return str(val)
    return None


def verify_token(token: str, client_id: str) -> bool:
    """Validate a token by calling Dhan's /v2/profile (same as DhanLiveClient.validate_token)."""
    if not token or not client_id:
        return False
    try:
        r = requests.get(
            _DHAN_PROFILE_URL,
            headers={"access-token": token, "client-id": client_id},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as exc:
        logger.warning("Token verification call failed: %s", exc)
        return False


def generate_access_token(
    *,
    client_id: str,
    pin: str,
    totp_secret: str,
    credentials_path: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], int]:
    """Single-POST headless login: client-id + PIN + TOTP -> access token -> credentials json."""
    output_path = _credentials_output_path(credentials_path)
    totp_code = _generate_totp(totp_secret)
    logger.info("Requesting Dhan access token for client %s (TOTP generated, valid ~%ds)",
                client_id, 30 - (int(time.time()) % 30))

    try:
        resp = requests.post(
            _DHAN_AUTH_URL,
            json={"dhanClientId": client_id, "pin": pin, "totp": totp_code},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
    except Exception as exc:
        logger.error("generateAccessToken POST failed: %s", exc)
        return None, 1

    if resp.status_code != 200:
        # Body may explain why (bad pin/totp, endpoint not enabled on tier, etc.).
        logger.error("generateAccessToken HTTP %s: %s", resp.status_code, resp.text[:300])
        return None, 1

    try:
        body = resp.json()
    except Exception:
        logger.error("generateAccessToken returned non-JSON: %s", resp.text[:300])
        return None, 1

    token = _extract_token(body)
    if not token:
        logger.error("No access token in response. Keys: %s", list(body.keys()) if isinstance(body, dict) else type(body))
        return None, 1

    cred = {
        "client_id": client_id,
        "access_token": token,
        "source": "dhan_totp_auth",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        backup = output_path.with_suffix(output_path.suffix + ".backup")
        backup.write_text(output_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    output_path.write_text(json.dumps(cred, indent=2), encoding="utf-8")
    logger.info("dhan_credentials.json written to %s", output_path)

    if not verify_token(token, client_id):
        logger.warning("Token written but /v2/profile verification failed")
        return cred, 1

    logger.info("Dhan token verified for client %s", client_id)
    return cred, 0


def update_env_compose(access_token: str) -> None:
    """Write DHAN_ACCESS_TOKEN into .env.compose (or .env) so compose picks it up."""
    if str(os.environ.get("DHAN_SKIP_ENV_UPDATE") or "0").strip() == "1":
        return
    env_path = _env_compose_path()
    if env_path is None:
        logger.warning("No .env.compose or .env found — skipping env update")
        return
    if _update_env_key(env_path, "DHAN_ACCESS_TOKEN", access_token):
        logger.info("DHAN_ACCESS_TOKEN updated in %s", env_path)
    else:
        logger.warning("Failed to update DHAN_ACCESS_TOKEN in %s", env_path)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Headless Dhan TOTP authentication for GCP VM.")
    parser.add_argument("--verify", action="store_true", help="Verify existing token and exit (no new login)")
    parser.add_argument("--dry-run", action="store_true", help="Print current TOTP code without calling Dhan")
    parser.add_argument("--credentials-path", default=None, help="Output path for dhan_credentials.json")
    args = parser.parse_args(argv)

    _load_dotenv_candidates()

    client_id = str(os.environ.get("DHAN_CLIENT_ID") or "").strip()
    pin = str(os.environ.get("DHAN_PIN") or "").strip()
    totp_secret = str(os.environ.get("DHAN_TOTP_SECRET") or "").strip()

    if args.dry_run:
        if not totp_secret:
            print("Error: DHAN_TOTP_SECRET not set", file=sys.stderr)
            return 1
        code = _generate_totp(totp_secret)
        print(f"Current TOTP code: {code}")
        print(f"Valid for ~{30 - (int(time.time()) % 30)} more seconds")
        return 0

    if args.verify:
        cred_path = _credentials_output_path(args.credentials_path)
        if not cred_path.exists():
            print("No dhan_credentials.json found", file=sys.stderr)
            return 1
        cred = json.loads(cred_path.read_text(encoding="utf-8-sig"))
        cid = client_id or str(cred.get("client_id") or "")
        if verify_token(str(cred.get("access_token") or ""), cid):
            print("Token valid")
            return 0
        print("Token invalid or expired")
        return 1

    missing = [k for k, v in [
        ("DHAN_CLIENT_ID", client_id),
        ("DHAN_PIN", pin),
        ("DHAN_TOTP_SECRET", totp_secret),
    ] if not v]
    if missing:
        print(f"Error: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env / .kite_secrets or export before running.", file=sys.stderr)
        return 1

    cred, code = generate_access_token(
        client_id=client_id,
        pin=pin,
        totp_secret=totp_secret,
        credentials_path=args.credentials_path,
    )
    if code == 0 and cred:
        update_env_compose(str(cred.get("access_token") or ""))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
