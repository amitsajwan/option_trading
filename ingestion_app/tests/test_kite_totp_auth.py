"""Unit tests for ingestion_app.kite_totp_auth (headless TOTP login)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion_app.kite_totp_auth import (
    _generate_totp,
    _update_env_key,
    login_headless,
    main,
)


# ── _generate_totp ────────────────────────────────────────────────────────────

def test_generate_totp_returns_6_digit_string():
    """A well-known TOTP secret should produce a 6-digit numeric string."""
    code = _generate_totp("JBSWY3DPEHPK3PXP")
    assert len(code) == 6
    assert code.isdigit()


def test_generate_totp_raises_on_bad_secret():
    with pytest.raises(Exception):
        _generate_totp("!!!invalid!!!")


# ── _update_env_key ───────────────────────────────────────────────────────────

def test_update_env_key_appends_new_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=foo\n", encoding="utf-8")
    result = _update_env_key(env_file, "NEW_KEY", "bar")
    assert result is True
    text = env_file.read_text(encoding="utf-8")
    assert "NEW_KEY=bar" in text
    assert "EXISTING=foo" in text


def test_update_env_key_replaces_existing_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KITE_ACCESS_TOKEN=old_value\n", encoding="utf-8")
    _update_env_key(env_file, "KITE_ACCESS_TOKEN", "new_value")
    text = env_file.read_text(encoding="utf-8")
    assert "new_value" in text
    assert "old_value" not in text


def test_update_env_key_creates_file_if_missing(tmp_path):
    env_file = tmp_path / "new.env"
    assert not env_file.exists()
    result = _update_env_key(env_file, "TOKEN", "abc123")
    assert result is True
    assert "TOKEN=abc123" in env_file.read_text(encoding="utf-8")


# ── CLI: --dry-run ────────────────────────────────────────────────────────────

def test_dry_run_prints_totp_code_and_exits_0(capsys, monkeypatch):
    monkeypatch.setenv("KITE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    code = main(["--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Current TOTP code:" in out
    assert "Valid for" in out


def test_dry_run_exits_1_without_totp_secret(capsys, monkeypatch):
    monkeypatch.delenv("KITE_TOTP_SECRET", raising=False)
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    code = main(["--dry-run"])
    assert code == 1


# ── CLI: --verify ─────────────────────────────────────────────────────────────

def test_verify_exits_1_if_no_credentials_file(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_CREDENTIALS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    code = main(["--verify"])
    assert code == 1


def test_verify_calls_validator_and_exits_0(tmp_path, monkeypatch):
    cred_path = tmp_path / "credentials.json"
    cred_path.write_text(json.dumps({"access_token": "tok123"}), encoding="utf-8")
    monkeypatch.setenv("KITE_CREDENTIALS_PATH", str(cred_path))
    monkeypatch.setenv("KITE_API_KEY", "testkey")
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")

    with patch("ingestion_app.kite_totp_auth.CredentialsValidator") as MockValidator:
        instance = MockValidator.return_value
        instance.verify_credentials.return_value = True
        code = main(["--verify"])

    assert code == 0
    instance.verify_credentials.assert_called_once_with("testkey", "tok123")


# ── login_headless ────────────────────────────────────────────────────────────

def _mock_session(step1_ok=True, step2_location=None, step2_body=None, step2_status=302):
    """Build a requests.Session mock that simulates Zerodha login flow."""
    session = MagicMock()

    # Step 1: POST /api/login
    login_resp = MagicMock()
    if step1_ok:
        login_resp.json.return_value = {
            "status": "success",
            "data": {"request_id": "req_abc123"},
        }
    else:
        login_resp.json.return_value = {"status": "error", "message": "invalid password"}
    login_resp.raise_for_status = MagicMock()

    # Step 2: POST /api/twofa
    twofa_resp = MagicMock()
    twofa_resp.status_code = step2_status
    twofa_resp.headers = {"Location": step2_location or ""}
    if step2_body is not None:
        twofa_resp.json.return_value = step2_body
    else:
        twofa_resp.json.side_effect = ValueError("no json")
    twofa_resp.text = ""

    session.post.side_effect = [login_resp, twofa_resp]
    session.headers = MagicMock()
    session.headers.update = MagicMock()
    return session


def test_login_headless_fails_if_step1_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    session = _mock_session(step1_ok=False)
    with patch("requests.Session", return_value=session):
        _, code = login_headless(
            user_id="U1", password="pw", totp_secret="JBSWY3DPEHPK3PXP",
            api_key="k", api_secret="s",
            credentials_path=str(tmp_path / "cred.json"),
        )
    assert code == 1


def test_login_headless_extracts_request_token_from_location_header(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    session = _mock_session(step2_location="https://app.kite.trade/?request_token=TOKXYZ123&status=success")

    kite_mock = MagicMock()
    kite_mock.generate_session.return_value = {
        "access_token": "ACCESS_TOK",
        "user_id": "U1",
    }

    with patch("requests.Session", return_value=session), \
         patch("ingestion_app.kite_totp_auth.create_kite_client", return_value=kite_mock), \
         patch("ingestion_app.kite_totp_auth.CredentialsValidator") as MockVal:
        MockVal.return_value.verify_credentials.return_value = True
        cred, code = login_headless(
            user_id="U1", password="pw", totp_secret="JBSWY3DPEHPK3PXP",
            api_key="k", api_secret="s",
            credentials_path=str(tmp_path / "cred.json"),
        )

    assert code == 0
    assert cred["access_token"] == "ACCESS_TOK"
    kite_mock.generate_session.assert_called_once_with("TOKXYZ123", api_secret="s")


def test_login_headless_extracts_request_token_from_body(tmp_path, monkeypatch):
    """Fallback: request_token in JSON body when Location header is absent."""
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    session = _mock_session(
        step2_location="",
        step2_body={"data": {"request_token": "BODYTOK789"}},
    )

    kite_mock = MagicMock()
    kite_mock.generate_session.return_value = {
        "access_token": "ACCESS_TOK2",
        "user_id": "U1",
    }

    with patch("requests.Session", return_value=session), \
         patch("ingestion_app.kite_totp_auth.create_kite_client", return_value=kite_mock), \
         patch("ingestion_app.kite_totp_auth.CredentialsValidator") as MockVal:
        MockVal.return_value.verify_credentials.return_value = True
        cred, code = login_headless(
            user_id="U1", password="pw", totp_secret="JBSWY3DPEHPK3PXP",
            api_key="k", api_secret="s",
            credentials_path=str(tmp_path / "cred.json"),
        )

    assert code == 0
    kite_mock.generate_session.assert_called_once_with("BODYTOK789", api_secret="s")


def test_login_headless_exits_1_if_no_request_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    # Both location and body are empty — should fail
    session = _mock_session(step2_location="", step2_body=None)

    with patch("requests.Session", return_value=session):
        _, code = login_headless(
            user_id="U1", password="pw", totp_secret="JBSWY3DPEHPK3PXP",
            api_key="k", api_secret="s",
            credentials_path=str(tmp_path / "cred.json"),
        )

    assert code == 1


def test_login_headless_writes_credentials_json(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    session = _mock_session(step2_location="https://x?request_token=TOKTOK")

    kite_mock = MagicMock()
    kite_mock.generate_session.return_value = {
        "access_token": "MY_ACCESS_TOKEN",
        "user_id": "BV2032",
    }

    cred_path = tmp_path / "credentials.json"

    with patch("requests.Session", return_value=session), \
         patch("ingestion_app.kite_totp_auth.create_kite_client", return_value=kite_mock), \
         patch("ingestion_app.kite_totp_auth.CredentialsValidator") as MockVal:
        MockVal.return_value.verify_credentials.return_value = True
        login_headless(
            user_id="BV2032", password="pw", totp_secret="JBSWY3DPEHPK3PXP",
            api_key="k", api_secret="s",
            credentials_path=str(cred_path),
        )

    assert cred_path.exists()
    data = json.loads(cred_path.read_text(encoding="utf-8"))
    assert data["access_token"] == "MY_ACCESS_TOKEN"
    assert data["user_id"] == "BV2032"
    assert data["api_key"] == "k"


def test_main_exits_1_on_missing_env_vars(capsys, monkeypatch):
    for var in ["KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KITE_SKIP_DOTENV_LOAD", "1")
    code = main([])
    assert code == 1
    err = capsys.readouterr().err
    assert "missing required env vars" in err
