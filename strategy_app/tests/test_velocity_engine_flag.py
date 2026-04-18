from unittest.mock import patch

from strategy_app.engines.deterministic_rule_engine import DeterministicRuleEngine
from strategy_app.engines.entry_policy import LongOptionEntryPolicy
from strategy_app.engines.regime import RegimeClassifier
from strategy_app.engines.velocity_entry_policy import VelocityEnhancedEntryPolicy
from strategy_app.engines.velocity_regime_classifier import VelocityEnhancedRegimeClassifier


def test_deterministic_engine_uses_plain_policy_by_default():
    with patch.dict("os.environ", {"STRATEGY_ENHANCED_VELOCITY": "0"}):
        engine = DeterministicRuleEngine()

    assert isinstance(engine._regime, RegimeClassifier)
    assert not isinstance(engine._regime, VelocityEnhancedRegimeClassifier)
    assert isinstance(engine._entry_policy, LongOptionEntryPolicy)
    assert not isinstance(engine._entry_policy, VelocityEnhancedEntryPolicy)


def test_deterministic_engine_uses_velocity_policy_when_enabled():
    with patch.dict("os.environ", {"STRATEGY_ENHANCED_VELOCITY": "1"}):
        engine = DeterministicRuleEngine()

    assert isinstance(engine._regime, VelocityEnhancedRegimeClassifier)
    assert isinstance(engine._entry_policy, VelocityEnhancedEntryPolicy)
