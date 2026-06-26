"""Tests for the headless Dhan TOTP auth module.

These never use real credentials — the TOTP secret here is the canonical RFC test
seed. Network calls are mocked; no live Dhan endpoint is hit.
"""
from __future__ import annotations

from unittest.mock import patch

from ingestion_app.dhan_totp_auth import (
    _extract_token,
    generate_access_token,
    main,
    verify_token,
)

_TEST_SECRET = "JBSWY3DPEHPK3PXP"  # RFC 6238 / pyotp doc test seed — NOT a real secret


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ── --dry-run CLI ────────────────────────────────────────────────────────────

def test_dry_run_prints_totp_code_and_exits_0(capsys, monkeypatch):
    monkeypatch.setenv("DHAN_TOTP_SECRET", _TEST_SECRET)
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    code = main(["--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Current TOTP code:" in out
    assert "Valid for" in out


def test_dry_run_exits_1_without_totp_secret(capsys, monkeypatch):
    monkeypatch.delenv("DHAN_TOTP_SECRET", raising=False)
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    assert main(["--dry-run"]) == 1


def test_main_exits_1_on_missing_env_vars(capsys, monkeypatch):
    for var in ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    assert main([]) == 1


# ── token extraction (tolerant of Dhan key naming) ───────────────────────────

def test_extract_token_top_level():
    assert _extract_token({"accessToken": "abc"}) == "abc"
    assert _extract_token({"access_token": "abc"}) == "abc"


def test_extract_token_nested_data():
    assert _extract_token({"data": {"accessToken": "xyz"}}) == "xyz"


def test_extract_token_none_when_absent():
    assert _extract_token({"status": "failure"}) is None
    assert _extract_token("not-a-dict") is None


# ── generate_access_token happy path (network + verify mocked) ────────────────

def test_generate_access_token_writes_and_verifies(tmp_path, monkeypatch):
    out = tmp_path / "dhan_credentials.json"
    with patch("ingestion_app.dhan_totp_auth.requests.post",
               return_value=_Resp(200, {"accessToken": "TOKEN123"})), \
         patch("ingestion_app.dhan_totp_auth.verify_token", return_value=True):
        cred, code = generate_access_token(
            client_id="1111", pin="000000", totp_secret=_TEST_SECRET,
            credentials_path=str(out),
        )
    assert code == 0
    assert cred["access_token"] == "TOKEN123"
    assert out.exists()


def test_generate_access_token_http_error_returns_1(tmp_path):
    with patch("ingestion_app.dhan_totp_auth.requests.post",
               return_value=_Resp(401, None, text="invalid pin")):
        cred, code = generate_access_token(
            client_id="1111", pin="bad", totp_secret=_TEST_SECRET,
            credentials_path=str(tmp_path / "c.json"),
        )
    assert code == 1
    assert cred is None


def test_verify_token_false_on_non_200():
    with patch("ingestion_app.dhan_totp_auth.requests.get", return_value=_Resp(403)):
        assert verify_token("tok", "1111") is False
