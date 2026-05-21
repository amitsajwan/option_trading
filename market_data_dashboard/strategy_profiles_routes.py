"""Strategy profile catalog for operator UI (Eval, Replay banners)."""

from __future__ import annotations

from fastapi import APIRouter

from strategy_app.engines.profiles import (
    PROFILE_DEBIT_MULTI_V1,
    PROFILE_DET_CORE_V2,
    PROFILE_DET_PROD_V1,
    PROFILE_DET_SETUP_V1,
    PROFILE_DET_V3_V1,
    PROFILE_PLAYBOOK_V1_PAPER_V1,
    PROFILE_R1S_TOP3_PAPER_V1,
    known_profile_ids,
    get_exit_strategies,
    get_regime_entry_map,
    get_risk_config,
)

REGIME_LABELS = (
    "TRENDING",
    "SIDEWAYS",
    "EXPIRY",
    "PRE_EXPIRY",
    "HIGH_VOL",
    "AVOID",
)

_PROFILE_META: dict[str, dict[str, str]] = {
    PROFILE_DEBIT_MULTI_V1: {
        "title": "Debit multi (buy CE/PE)",
        "summary": "TRENDING → long CE; SIDEWAYS → long PE. Brain gates entries.",
        "operator_focus": "true",
    },
    PROFILE_R1S_TOP3_PAPER_V1: {
        "title": "R1S top-3 short CE",
        "summary": "Short premium CE playbook across regimes (research baseline).",
    },
    PROFILE_PLAYBOOK_V1_PAPER_V1: {
        "title": "Playbook v1 thesis",
        "summary": "PBV1_TOP3_THESIS; exits via PlaybookBrain.",
    },
    PROFILE_DET_PROD_V1: {
        "title": "Deterministic prod v1",
        "summary": "ORB / OI buildup book with trailing risk.",
    },
    PROFILE_DET_CORE_V2: {
        "title": "Deterministic core v2",
        "summary": "ORB, VWAP reclaim, prev-day level variants.",
    },
    PROFILE_DET_SETUP_V1: {
        "title": "Deterministic setup v1",
        "summary": "TRADER_COMPOSITE entries.",
    },
    PROFILE_DET_V3_V1: {
        "title": "Deterministic v3 v1",
        "summary": "TRADER_V3_COMPOSITE entries.",
    },
}


def build_profiles_catalog() -> dict:
    """Serialize profiles.py for dashboard clients."""
    profiles: list[dict] = []
    all_strategies: set[str] = set()

    for profile_id in sorted(known_profile_ids()):
        regime_map = get_regime_entry_map(profile_id)
        exit_strategies = get_exit_strategies(profile_id)
        risk = get_risk_config(profile_id)
        meta = _PROFILE_META.get(profile_id, {})
        entry_ids: set[str] = set(exit_strategies)
        for strategies in regime_map.values():
            for sid in strategies:
                if sid and sid != "IV_FILTER":
                    entry_ids.add(sid)
        all_strategies.update(entry_ids)
        profiles.append(
            {
                "profile_id": profile_id,
                "title": meta.get("title") or profile_id,
                "summary": meta.get("summary") or "",
                "operator_focus": meta.get("operator_focus") == "true",
                "regime_entry_map": regime_map,
                "exit_strategies": exit_strategies,
                "entry_strategy_ids": sorted(entry_ids),
                "risk_config": risk,
            }
        )

    return {
        "regimes": list(REGIME_LABELS),
        "profiles": profiles,
        "all_entry_strategy_ids": sorted(all_strategies),
        "default_operator_profile_id": PROFILE_DEBIT_MULTI_V1,
    }


class StrategyProfilesRouter:
    def __init__(self) -> None:
        router = APIRouter(tags=["strategy-profiles"])
        router.add_api_route(
            "/api/strategy/profiles/catalog",
            self.get_catalog,
            methods=["GET"],
        )
        self.router = router

    async def get_catalog(self) -> dict:
        return build_profiles_catalog()


__all__ = ["StrategyProfilesRouter", "build_profiles_catalog"]
