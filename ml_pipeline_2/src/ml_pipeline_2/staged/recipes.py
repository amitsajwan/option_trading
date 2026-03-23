from __future__ import annotations

from typing import Dict, List

from ..catalog.research_defaults import DEFAULT_STAGED_RECIPES
from ..contracts.types import LabelRecipe

FIXED_RECIPE_CATALOG_ID = "fixed_l0_l3_v1"


def recipe_catalogs_by_id() -> Dict[str, List[LabelRecipe]]:
    return {
        FIXED_RECIPE_CATALOG_ID: [LabelRecipe(**recipe.to_dict()) for recipe in DEFAULT_STAGED_RECIPES],
    }


def recipe_catalog_ids() -> list[str]:
    return sorted(recipe_catalogs_by_id())


def get_recipe_catalog(recipe_catalog_id: str) -> list[LabelRecipe]:
    normalized = str(recipe_catalog_id or "").strip()
    catalogs = recipe_catalogs_by_id()
    if normalized not in catalogs:
        raise ValueError(f"unknown recipe_catalog_id: {recipe_catalog_id}; valid options: {recipe_catalog_ids()}")
    return [LabelRecipe(**recipe.to_dict()) for recipe in catalogs[normalized]]
