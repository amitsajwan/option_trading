from __future__ import annotations

from pathlib import Path


def test_ml_pipeline_2_does_not_import_ml_pipeline() -> None:
    root = Path("ml_pipeline_2/src/ml_pipeline_2")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import ml_pipeline" in text or "from ml_pipeline" in text:
            offenders.append(str(path))
    assert offenders == []


def test_scenario_flows_do_not_import_each_other() -> None:
    root = Path("ml_pipeline_2/src/ml_pipeline_2/scenario_flows")
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "from .phase2_label_sweep" in text or "import .phase2_label_sweep" in text:
            offenders.append(str(path))
        if "from .fo_expiry_aware_recovery" in text or "import .fo_expiry_aware_recovery" in text:
            offenders.append(str(path))
    assert offenders == []

