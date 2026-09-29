"""Tests for ops/gcp/dhan_totp_refresh.py -- the account-switch path.

Switching to a new Dhan account must only require replacing .env.totp, so the
refresh has to write the client ID (not just the token) into .env.compose, and
must refuse to run without an explicit client ID rather than fall back to a
hardcoded one.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "ops" / "gcp" / "dhan_totp_refresh.py"
_spec = importlib.util.spec_from_file_location("dhan_totp_refresh", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_replaces_existing_keys_in_place():
    text = "A=1\nDHAN_CLIENT_ID=OLD\n# comment\nDHAN_ACCESS_TOKEN=oldtok\nZ=9\n"
    out = mod.set_env_keys(text, {"DHAN_CLIENT_ID": "NEW", "DHAN_ACCESS_TOKEN": "newtok"})
    assert out == "A=1\nDHAN_CLIENT_ID=NEW\n# comment\nDHAN_ACCESS_TOKEN=newtok\nZ=9\n"


def test_appends_missing_keys():
    out = mod.set_env_keys("A=1\n", {"DHAN_CLIENT_ID": "NEW", "DHAN_ACCESS_TOKEN": "tok"})
    assert out == "A=1\nDHAN_CLIENT_ID=NEW\nDHAN_ACCESS_TOKEN=tok\n"


def test_does_not_touch_prefix_lookalike_keys():
    # SENSEX_DHAN_CLIENT_ID must not be rewritten when setting DHAN_CLIENT_ID.
    text = "SENSEX_DHAN_CLIENT_ID=keep\nDHAN_CLIENT_ID=OLD\n"
    out = mod.set_env_keys(text, {"DHAN_CLIENT_ID": "NEW"})
    assert "SENSEX_DHAN_CLIENT_ID=keep" in out
    assert "\nDHAN_CLIENT_ID=NEW\n" in out


def test_value_with_regex_special_chars_is_written_literally():
    # Tokens are JWTs; backslashes/group refs in a value must not be interpreted.
    out = mod.set_env_keys("DHAN_ACCESS_TOKEN=x\n", {"DHAN_ACCESS_TOKEN": r"ab\1.c$d"})
    assert out == "DHAN_ACCESS_TOKEN=ab\\1.c$d\n"


def test_missing_client_id_fails_instead_of_falling_back(tmp_path, monkeypatch, capsys):
    totp = tmp_path / ".env.totp"
    totp.write_text("DHAN_PIN=1234\nDHAN_TOTP_SECRET=JBSWY3DPEHPK3PXP\n")
    monkeypatch.setattr(mod, "ENVTOTP", str(totp))

    def _no_network(*a, **k):
        raise AssertionError("must fail before any network call")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _no_network)
    assert mod.main() == 1
    assert "DHAN_CLIENT_ID" in capsys.readouterr().out


def test_no_hardcoded_client_id_in_source():
    src = _PATH.read_text(encoding="utf-8")
    assert "or \"1111957145\"" not in src
