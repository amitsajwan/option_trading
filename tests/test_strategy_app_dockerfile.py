from pathlib import Path


def test_strategy_app_dockerfile_includes_snapshot_app_package() -> None:
    dockerfile = Path("strategy_app/Dockerfile").read_text(encoding="utf-8")
    assert "COPY snapshot_app /app/snapshot_app" in dockerfile
