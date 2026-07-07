#!/usr/bin/env python3
"""Headless Dhan token refresh via TOTP (stdlib only — no pip deps on the host).

Reads DHAN_CLIENT_ID/DHAN_PIN/DHAN_TOTP_SECRET from /opt/option_trading/.env.totp,
generates a fresh 24h access token, and writes DHAN_ACCESS_TOKEN into
/opt/option_trading/.env.compose. Robust: regenerates from cold (no dependency on
a prior valid token). Never prints secrets or the token.
"""
import base64, hashlib, hmac, json, os, re, struct, sys, time, urllib.parse, urllib.request

ENVC = "/opt/option_trading/.env.compose"
ENVTOTP = "/opt/option_trading/.env.totp"
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"


def _read_env(path):
    d = {}
    if os.path.exists(path):
        for ln in open(path):
            ln = ln.strip()
            if "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def _totp(secret):
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret.strip().upper() + pad)
    msg = struct.pack(">Q", int(time.time()) // 30)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o+4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def main():
    t = _read_env(ENVTOTP)
    cid = t.get("DHAN_CLIENT_ID") or "1111957145"
    pin = t.get("DHAN_PIN")
    secret = t.get("DHAN_TOTP_SECRET")
    if not pin or not secret:
        print("FAILED: DHAN_PIN / DHAN_TOTP_SECRET missing in .env.totp")
        return 1

    # Dhan expects the params as QUERY STRING on a POST (not a JSON body).
    url = AUTH_URL + "?" + urllib.parse.urlencode(
        {"dhanClientId": cid, "pin": pin, "totp": _totp(secret)}
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as exc:
        print(f"FAILED: generateAccessToken error: {exc}")
        return 1

    tok = None
    for c in (resp, resp.get("data") or {}):
        if isinstance(c, dict):
            for k in ("accessToken", "access_token", "token"):
                if c.get(k):
                    tok = str(c[k])
    if not tok:
        print("FAILED: no token in response:", json.dumps(resp)[:200])
        return 1

    s = open(ENVC).read()
    if re.search(r"^DHAN_ACCESS_TOKEN=", s, re.M):
        s = re.sub(r"^DHAN_ACCESS_TOKEN=.*$", "DHAN_ACCESS_TOKEN=" + tok, s, flags=re.M)
    else:
        s = s.rstrip("\n") + "\nDHAN_ACCESS_TOKEN=" + tok + "\n"
    open(ENVC, "w").write(s)
    print(f"OK: fresh Dhan token minted via TOTP and written to .env.compose (len={len(tok)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
