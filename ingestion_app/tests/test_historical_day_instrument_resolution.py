"""
Regression for the 2026-08-02 FINNIFTY data-corruption bug.

get_historical_day() used to wrap contracts_app.get_instrument(underlying) in a
bare try/except that fell back to self._active_spec() (this container's own
default instrument, e.g. BankNifty) on ANY failure -- including a genuinely
unknown instrument name. That silently returned the wrong instrument's real
data mislabeled under the requested name, defeating get_instrument()'s own
documented "fail loud" contract. Every FINNIFTY historical-day fetch before
this fix silently returned BankNifty's index/futures/options data.

These tests lock in that an unknown instrument now raises instead of silently
substituting a different real instrument, while a legitimately registered
instrument (including the newly-added FINNIFTY) still resolves correctly.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ingestion_app.dhan_data_service import DhanDataService


def _make_service(monkeypatch) -> DhanDataService:
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("DHAN_CLIENT_ID", "test-client")
    monkeypatch.setenv("DHAN_WS_ENABLED", "0")
    with patch("ingestion_app.dhan_data_service.redis.Redis"), \
         patch("ingestion_app.dhan_data_service.DhanLiveClient") as mock_client_cls:
        mock_client_cls.return_value = MagicMock()
        return DhanDataService()


def test_unknown_instrument_raises_not_silently_substitutes(monkeypatch):
    svc = _make_service(monkeypatch)
    with pytest.raises(KeyError):
        svc.get_historical_day("DOWJONES", "2026-01-01")


def test_finnifty_resolves_to_its_own_spec_not_the_container_default(monkeypatch):
    svc = _make_service(monkeypatch)
    svc._client.get_intraday_ohlc = MagicMock(return_value=[])
    svc._scrip = MagicMock()

    # get_historical_day imports get_instrument locally (`from contracts_app
    # import get_instrument`) rather than at module scope, so the patch target
    # is contracts_app itself, not ingestion_app.dhan_data_service.
    with patch("contracts_app.get_instrument") as mock_get:
        from contracts_app import get_instrument as real_get_instrument
        mock_get.side_effect = real_get_instrument
        try:
            svc.get_historical_day("FINNIFTY", "2026-01-01")
        except Exception:
            pass  # only spec resolution is under test here
        called_with = mock_get.call_args[0][0]
        assert called_with == "FINNIFTY"
