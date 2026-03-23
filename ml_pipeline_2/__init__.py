from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parent
_SRC_PACKAGE = _ROOT / "src" / "ml_pipeline_2"

if _SRC_PACKAGE.exists():
    __path__.append(str(_SRC_PACKAGE))

