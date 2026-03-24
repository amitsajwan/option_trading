from pathlib import Path


def test_historical_strategy_compose_exposes_ml_pure_inputs() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert 'ML_PURE_RUN_ID: "${ML_PURE_RUN_ID:-}"' in compose
    assert 'ML_PURE_MODEL_GROUP: "${ML_PURE_MODEL_GROUP:-}"' in compose
    assert '"${STRATEGY_ROLLOUT_STAGE_HISTORICAL:-paper}"' in compose
    assert '"${STRATEGY_POSITION_SIZE_MULTIPLIER_HISTORICAL:-1.0}"' in compose
    assert "./ml_pipeline_2/artifacts:/app/ml_pipeline_2/artifacts:ro" in compose
