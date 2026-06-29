"""Tests for contracts_app.instruments — the shared instrument registry.

This registry is the single source of truth that ingestion / strategy /
execution / training all import. Keep it aligned with
ml_pipeline_2/scripts/dhan_data_pipeline.py::INSTRUMENTS.
"""
from __future__ import annotations

import unittest

from contracts_app import get_instrument, known_instruments, INSTRUMENTS, PRIMARY_INSTRUMENT


class TestRegistry(unittest.TestCase):
    def test_known_instruments(self) -> None:
        self.assertIn("BANKNIFTY", known_instruments())
        self.assertIn("NIFTY", known_instruments())

    def test_banknifty_facts(self) -> None:
        spec = get_instrument("BANKNIFTY")
        self.assertEqual(spec.index_security_id, "25")
        self.assertEqual(spec.lot_size, 30)
        self.assertEqual(spec.strike_step, 100)
        self.assertEqual(spec.expiry_cadence, "monthly")

    def test_nifty_facts(self) -> None:
        spec = get_instrument("NIFTY")
        self.assertEqual(spec.index_security_id, "13")
        self.assertEqual(spec.lot_size, 75)
        self.assertEqual(spec.strike_step, 50)
        self.assertEqual(spec.expiry_cadence, "weekly")

    def test_default_is_primary(self) -> None:
        self.assertEqual(get_instrument(None).name, PRIMARY_INSTRUMENT)
        self.assertEqual(get_instrument("").name, PRIMARY_INSTRUMENT)

    def test_case_insensitive(self) -> None:
        self.assertEqual(get_instrument("nifty").name, "NIFTY")
        self.assertEqual(get_instrument("BankNifty").name, "BANKNIFTY")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            get_instrument("DOWJONES")

    def test_registry_parity_with_pipeline(self) -> None:
        # The live registry must match the training pipeline's definition for
        # the fields that affect train/serve alignment.
        try:
            import importlib
            mod = importlib.import_module("ml_pipeline_2.scripts.dhan_data_pipeline")
        except Exception:
            self.skipTest("ml_pipeline_2 not importable in this env")
        for name, pipe_cfg in mod.INSTRUMENTS.items():
            spec = INSTRUMENTS.get(name)
            self.assertIsNotNone(spec, f"{name} missing from contracts registry")
            self.assertEqual(spec.lot_size, pipe_cfg.lot_size, f"{name} lot_size skew")
            self.assertEqual(spec.strike_step, pipe_cfg.strike_step, f"{name} strike_step skew")
            self.assertEqual(spec.expiry_cadence, pipe_cfg.expiry_cadence, f"{name} cadence skew")
            self.assertEqual(spec.index_security_id, pipe_cfg.index_security_id,
                             f"{name} index_security_id skew")


if __name__ == "__main__":
    unittest.main()
