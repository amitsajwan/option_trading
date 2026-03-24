from pathlib import Path


def test_runtime_startup_template_supports_local_build_image_source() -> None:
    template = Path("infra/gcp/templates/runtime-startup.sh.tftpl").read_text(encoding="utf-8")
    assert 'IMAGE_SOURCE="$${IMAGE_SOURCE:-ghcr}"' in template
    assert 'COMPOSE_ARGS=(-f docker-compose.yml)' in template
    assert 'docker compose "$${COMPOSE_ARGS[@]}" build ingestion_app snapshot_app persistence_app strategy_app' in template
