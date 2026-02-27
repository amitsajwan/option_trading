from dataclasses import dataclass


@dataclass(frozen=True)
class LabelConfig:
    horizon_minutes: int = 3
    return_threshold: float = 0.002  # 0.20%
    use_excursion_gate: bool = False
    min_favorable_excursion: float = 0.002
    max_adverse_excursion: float = 0.001


@dataclass(frozen=True)
class TrainConfig:
    train_ratio: float = 0.70
    valid_ratio: float = 0.15
    random_state: int = 42
    max_depth: int = 4
    n_estimators: int = 400
    learning_rate: float = 0.03


@dataclass(frozen=True)
class DecisionConfig:
    threshold_min: float = 0.50
    threshold_max: float = 0.90
    threshold_step: float = 0.01
    cost_per_trade: float = 0.0006

