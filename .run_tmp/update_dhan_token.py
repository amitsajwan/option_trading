"""Refresh the Dhan access token via TOTP and write it into .env.compose.

Replaces the old hand-paste flow. Generates a fresh 24h token headlessly using
Dhan's TOTP-enabled endpoint (the Kite-pyotp analog), then updates
DHAN_ACCESS_TOKEN in the compose env file. Intended to run on a cron at ~09:00
IST (or on container start), followed by a restart of the execution container.

Required env (keep these as secrets, NOT in the repo):
  DHAN_CLIENT_ID    — numeric client id (e.g. 1111957145)
  DHAN_PIN          — your Dhan login PIN
  DHAN_TOTP_SECRET  — base32 secret shown (QR/text) when you enable TOTP for the
                      API account under Profile -> DhanHQ APIs / security

Optional env:
  DHAN_ENV_FILE     — path to compose env file (default /opt/option_trading/.env.compose)

Enable TOTP once in the Dhan portal before using this. The endpoint:
  POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=..&pin=..&totp=..
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = os.environ.get("DHAN_ENV_FILE", "/opt/option_trading/.env.compose")
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set (export it as a secret before running)")
    return val


def totp_now(secret: str, digits: int = 6, period: int = 30) -> str:
    """Standard RFC-6238 TOTP (HMAC-SHA1), stdlib only — no pyotp."""
    key = base64.b32decode(secret.strip().replace(" ", "").upper())
    counter = struct.pack(">Q", int(time.time()) // period)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def fetch_token() -> str:
    client_id = _require("DHAN_CLIENT_ID")
    pin = _require("DHAN_PIN")
    secret = _require("DHAN_TOTP_SECRET")

    params = urllib.parse.urlencode(
        {"dhanClientId": client_id, "pin": pin, "totp": totp_now(secret)}
    )
    req = urllib.request.Request(f"{AUTH_URL}?{params}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: token request failed ({e.code}): {e.read().decode()[:300]}")
    # Field name not guaranteed across versions; check the common shapes.
    data = body.get("data", body) if isinstance(body, dict) else {}
    token = (
        data.get("accessToken")
        or data.get("access_token")
        or data.get("token")
    )
    if not token:
        sys.exit(f"ERROR: no access token in response: {body}")
    expiry = data.get("expiryTime") or data.get("expiry") or "unknown"
    print(f"Fetched Dhan token (expires: {expiry})")
    return token


def write_token(token: str) -> None:
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        sys.exit(f"ERROR: env file not found: {ENV_FILE}")

    updated, found = [], False
    for line in lines:
        if line.startswith("DHAN_ACCESS_TOKEN="):
            updated.append(f"DHAN_ACCESS_TOKEN={token}\n")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"DHAN_ACCESS_TOKEN={token}\n")

    with open(ENV_FILE, "w") as f:
        f.writelines(updated)
    print(f"DHAN_ACCESS_TOKEN updated in {ENV_FILE}")


if __name__ == "__main__":
    write_token(fetch_token())
    print("Done. Restart the execution container to pick up the new token.")
