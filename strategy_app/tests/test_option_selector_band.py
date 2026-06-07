"""Band-mode deep-OTM reachability (operator: 'go with 12 OTM steps')."""
from __future__ import annotations

import os
import unittest

from strategy_app.signals import option_selector as sel


class BandModeDepthTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("STRATEGY_STRIKE_MAX_OTM_STEPS", "SMART_STRIKE_OTM2_ENABLED",
                        "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_band_mode_scans_all_depths_without_enabled_flags(self):
        # The live-bug repro: MAX_OTM_STEPS=12 but NO per-tier _ENABLED flags set.
        os.environ["STRATEGY_STRIKE_MAX_OTM_STEPS"] = "12"
        for k in ("SMART_STRIKE_OTM2_ENABLED", "SMART_STRIKE_OTM3_ENABLED", "SMART_STRIKE_OTM4_ENABLED"):
            os.environ.pop(k, None)
        # gate-mode (all_depths=False): only tier 1 is built (the bug)
        gate = sel._build_otm_tiers(all_depths=False)
        self.assertEqual([t.n for t in gate], [1])
        # band-mode (all_depths=True): all 12 depths reachable, deepest-first
        band = sel._build_otm_tiers(all_depths=True)
        self.assertEqual([t.n for t in band], list(range(12, 0, -1)))

    def test_max_otm_steps_one_is_the_cap_even_in_band(self):
        os.environ["STRATEGY_STRIKE_MAX_OTM_STEPS"] = "1"   # the regressed live value
        self.assertEqual([t.n for t in sel._build_otm_tiers(all_depths=True)], [1])


if __name__ == "__main__":
    unittest.main()
