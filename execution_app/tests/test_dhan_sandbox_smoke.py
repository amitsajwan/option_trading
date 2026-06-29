from __future__ import annotations

import importlib
import sys
from datetime import date, datetime

from strategy_app.contracts import SignalType, TradeSignal


class _FakeScrips:
    def resolve(self, expiry: date, strike: int, option_type: str):
        assert expiry == date(2026, 7, 2)
        assert strike == 24000
        assert option_type == "CE"
        return "12345", 75


def test_dhan_adapter_uses_nifty_lot_and_sandbox_base(monkeypatch):
    monkeypatch.setenv("STRATEGY_INSTRUMENT", "NIFTY")
    monkeypatch.delenv("STRATEGY_LOT_SIZE", raising=False)
    monkeypatch.setenv("DHAN_API_BASE", "https://sandbox.dhan.co/v2")

    import execution_app.adapter.dhan as dhan_module

    dhan_module = importlib.reload(dhan_module)
    adapter = dhan_module.DhanAdapter(client_id="client", access_token="token")
    adapter._scrips = _FakeScrips()

    captured = {}

    def fake_request(method, path, body=None):
        captured.update(method=method, path=path, body=body)
        return 201, {"orderId": "sandbox-order-1", "orderStatus": "TRANSIT"}

    adapter._request = fake_request
    signal = TradeSignal(
        signal_id="sig-1",
        timestamp=datetime(2026, 7, 1, 9, 30),
        snapshot_id="snap-1",
        signal_type=SignalType.ENTRY,
        direction="CE",
        strike=24000,
        expiry=date(2026, 7, 2),
        max_lots=1,
    )

    result = adapter.place_entry(signal)

    assert adapter._base == "https://sandbox.dhan.co/v2"
    assert result.order_id == "sandbox-order-1"
    assert captured["method"] == "POST"
    assert captured["path"] == "/orders"
    assert captured["body"]["securityId"] == "12345"
    assert captured["body"]["quantity"] == 75


def test_sandbox_smoke_refuses_live_dhan_base(monkeypatch, capsys):
    monkeypatch.setenv("DHAN_API_BASE", "https://api.dhan.co/v2")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dhan_sandbox_smoke.py",
            "--expiry",
            "2026-07-02",
            "--strike",
            "24000",
            "--direction",
            "CE",
        ],
    )

    from ops.gcp import dhan_sandbox_smoke

    assert dhan_sandbox_smoke.main() == 2
    captured = capsys.readouterr()
    assert "refusing to run outside Dhan sandbox" in captured.err
    assert "https://api.dhan.co/v2" in captured.err
