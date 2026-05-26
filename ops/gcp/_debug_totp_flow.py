"""Debug script: run on VM to trace every step of the Zerodha TOTP login flow.

Usage on VM:
  cd /opt/option_trading
  sudo bash -c 'source .env.totp && .venv/bin/python ops/gcp/_debug_totp_flow.py'
"""
from __future__ import annotations
import os, re, sys
import requests
import pyotp
from kiteconnect import KiteConnect

api_key     = os.environ["KITE_API_KEY"]
user_id     = os.environ["KITE_USER_ID"]
password    = os.environ["KITE_PASSWORD"]
totp_secret = os.environ["KITE_TOTP_SECRET"]

print("=" * 60)
print("KITE TOTP LOGIN FLOW DEBUG")
print("=" * 60)

session = requests.Session()
session.headers.update({"X-Kite-Version": "3"})

kite = KiteConnect(api_key=api_key)
login_url = kite.login_url()
print(f"\n[Step 0] Visiting KiteConnect login URL")
print(f"  URL: {login_url}")

resp0 = session.get(login_url, allow_redirects=True)
print(f"  Final URL after redirects: {resp0.url}")
print(f"  Status: {resp0.status_code}")
print(f"  Cookies after Step 0: {dict(session.cookies)}")

print(f"\n[Step 1] POST /api/login as {user_id}")
resp1 = session.post(
    "https://kite.zerodha.com/api/login",
    data={"user_id": user_id, "password": password},
    timeout=15,
)
print(f"  Status: {resp1.status_code}")
print(f"  Body: {resp1.text[:300]}")
body1 = resp1.json()
if body1.get("status") != "success":
    print("FAILED at Step 1 — stopping")
    sys.exit(1)
request_id = body1["data"]["request_id"]
print(f"  request_id: {request_id}")
print(f"  Cookies after Step 1: {dict(session.cookies)}")

totp_code = pyotp.TOTP(totp_secret).now()
print(f"\n[Step 2] POST /api/twofa (TOTP={totp_code}, allow_redirects=False)")
resp2 = session.post(
    "https://kite.zerodha.com/api/twofa",
    data={"user_id": user_id, "request_id": request_id,
          "twofa_value": totp_code, "twofa_type": "totp"},
    timeout=15,
    allow_redirects=False,
)
print(f"  Status: {resp2.status_code}")
print(f"  Location: {resp2.headers.get('Location', '(none)')}")
print(f"  Body: {resp2.text[:300]}")
print(f"  Cookies after Step 2: {dict(session.cookies)}")

# Extract sess_id from Step 0 final URL
sess_id = None
m0 = re.search(r"sess_id=([^&]+)", resp0.url or "")
if m0:
    sess_id = m0.group(1)
    print(f"\n  sess_id extracted from Step 0 URL: {sess_id}")

print(f"\n[Step 2.5a] GET /connect/finish (no params, allow_redirects=False)")
resp_finish = session.get(
    "https://kite.zerodha.com/connect/finish",
    timeout=15,
    allow_redirects=False,
)
print(f"  Status: {resp_finish.status_code}")
print(f"  Location: {resp_finish.headers.get('Location', '(none)')}")
print(f"  Body: {resp_finish.text[:300]}")

if sess_id:
    print(f"\n[Step 2.5b] GET /connect/finish?sess_id={sess_id[:10]}... (allow_redirects=False)")
    resp_finish2 = session.get(
        f"https://kite.zerodha.com/connect/finish?sess_id={sess_id}",
        timeout=15,
        allow_redirects=False,
    )
    print(f"  Status: {resp_finish2.status_code}")
    print(f"  Location: {resp_finish2.headers.get('Location', '(none)')}")
    print(f"  Body: {resp_finish2.text[:300]}")

    print(f"\n[Step 2.5c] GET /connect/finish?api_key={api_key}&sess_id={sess_id[:10]}...")
    resp_finish3 = session.get(
        f"https://kite.zerodha.com/connect/finish?api_key={api_key}&sess_id={sess_id}",
        timeout=15,
        allow_redirects=False,
    )
    print(f"  Status: {resp_finish3.status_code}")
    print(f"  Location: {resp_finish3.headers.get('Location', '(none)')}")
    print(f"  Body: {resp_finish3.text[:300]}")

print("\n--- Summary of cookies at end ---")
for k, v in session.cookies.items():
    print(f"  {k}: {v[:40]}...")
