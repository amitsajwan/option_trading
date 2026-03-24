from __future__ import annotations

from persistence_app import strategy_health


def test_strategy_health_offline_mode_is_healthy_with_persisted_docs_but_no_latest_timestamp(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_ENABLED", "0")
    monkeypatch.setattr(strategy_health, "find_matching_python_processes", lambda patterns: [(123, "python -m persistence_app.main_strategy_consumer")])
    monkeypatch.setattr(
        strategy_health,
        "_mongo_latest",
        lambda: (
            True,
            {
                "db": "trading_ai",
                "collections": {
                    "strategy_votes": "strategy_votes_historical",
                    "trade_signals": "trade_signals_historical",
                    "strategy_positions": "strategy_positions_historical",
                },
                "counts": {
                    "strategy_votes": 0,
                    "trade_signals": 375,
                    "strategy_positions": 0,
                },
                "total_docs": 375,
                "latest": None,
            },
            None,
        ),
    )

    payload, code = strategy_health.evaluate(max_age_seconds=300.0)

    assert code == 0
    assert payload["status"] == "healthy"
    assert payload["mongo"]["total_docs"] == 375


def test_strategy_health_mongo_latest_accepts_string_timestamps(monkeypatch) -> None:
    class _Collection:
        def __init__(self, doc: dict[str, object], count: int) -> None:
            self._doc = doc
            self._count = count

        def count_documents(self, _query: dict[str, object]) -> int:
            return self._count

        def find_one(self, _query: dict[str, object], _projection: dict[str, int], sort: list[tuple[str, int]]) -> dict[str, object]:
            assert sort == [("trade_date_ist", -1), ("market_time_ist", -1), ("timestamp", -1)]
            return dict(self._doc)

    class _Database:
        def __getitem__(self, name: str) -> _Collection:
            if name == "trade_signals":
                return _Collection(
                    {
                        "event_type": "trade_signal",
                        "trade_date_ist": "2024-10-31",
                        "market_time_ist": "15:29:59",
                        "timestamp": "2024-10-31T15:29:59+05:30",
                    },
                    375,
                )
            return _Collection({}, 0)

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        @property
        def admin(self) -> "_Client":
            return self

        def command(self, _name: str) -> None:
            return None

        def __getitem__(self, _name: str) -> _Database:
            return _Database()

    monkeypatch.setattr(strategy_health, "MongoClient", _Client)

    ok, payload, error = strategy_health._mongo_latest()

    assert ok is True
    assert error is None
    assert payload["total_docs"] == 375
    assert payload["collection"] == "trade_signals"
    assert payload["latest"]["timestamp"] == "2024-10-31T15:29:59+05:30"
