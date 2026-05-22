"""Tests for strategy profile catalog API."""

from market_data_dashboard.routes.strategy_profiles_routes import build_profiles_catalog


def test_build_profiles_catalog_includes_debit_multi():
    payload = build_profiles_catalog()
    ids = {p["profile_id"] for p in payload["profiles"]}
    assert "debit_multi_v1" in ids
    assert "TRENDING" in payload["regimes"]
    debit = next(p for p in payload["profiles"] if p["profile_id"] == "debit_multi_v1")
    assert "R2_TOP3_LONG_CE" in debit["regime_entry_map"]["TRENDING"]
    assert "R1_TOP3_LONG_PE" in debit["regime_entry_map"]["SIDEWAYS"]
    assert payload["default_operator_profile_id"] == "debit_multi_v1"
    assert set(debit["entry_strategy_ids"]) == {"R1_TOP3_LONG_PE", "R2_TOP3_LONG_CE"}
    assert "ORB" in debit["exit_strategies"]
    assert "ORB" not in debit["entry_strategy_ids"]
