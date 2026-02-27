import unittest

from ml_pipeline.order_intent_runtime import (
    GuardConfig,
    build_order_intents,
    run_order_intent_runtime,
)


def _decision_event(
    *,
    ts: str,
    event_type: str,
    side: str,
    action: str,
    symbol: str = "BANKNIFTY04SEP2451600CE",
    price: float = 100.0,
) -> dict:
    side_u = str(side).upper()
    price_key = "opt_0_ce_close" if side_u == "CE" else "opt_0_pe_close"
    return {
        "timestamp": ts,
        "event_type": event_type,
        "action": action,
        "position": {"side": side_u, "entry_timestamp": ts, "entry_confidence": 0.7},
        "position_runtime": {
            "side": side_u,
            "qty": 10.0,
            "entry_price": 100.0,
            "option_symbol": symbol,
        },
        "prices": {price_key: price},
    }


class OrderIntentRuntimeTests(unittest.TestCase):
    def test_dedupe_and_replay_safety(self) -> None:
        events = [
            _decision_event(ts="2024-09-03T09:39:00+05:30", event_type="ENTRY", side="CE", action="BUY_CE", price=101.0),
            _decision_event(ts="2024-09-03T09:39:00+05:30", event_type="ENTRY", side="CE", action="BUY_CE", price=101.0),
            _decision_event(ts="2024-09-03T09:41:00+05:30", event_type="EXIT", side="CE", action="HOLD", price=98.0),
        ]
        r1 = run_order_intent_runtime(events, guard_cfg=GuardConfig(max_unmatched_intent_share=1.0, max_side_mismatch_share=1.0, max_consecutive_losses=99, max_drawdown=1.0))
        r2 = run_order_intent_runtime(events, guard_cfg=GuardConfig(max_unmatched_intent_share=1.0, max_side_mismatch_share=1.0, max_consecutive_losses=99, max_drawdown=1.0))
        self.assertEqual(r1["intent_counts"]["raw"], 3)
        self.assertEqual(r1["intent_counts"]["deduped"], 2)
        self.assertEqual(r1["intent_counts"]["duplicates_dropped"], 1)
        self.assertEqual(r1["intent_counts"], r2["intent_counts"])
        self.assertEqual(r1["reconciliation"]["matched_intents"], r2["reconciliation"]["matched_intents"])

    def test_mismatch_accounting_for_missing_fill(self) -> None:
        decisions = [
            _decision_event(ts="2024-09-03T09:39:00+05:30", event_type="ENTRY", side="CE", action="BUY_CE", price=102.0),
            _decision_event(ts="2024-09-03T09:41:00+05:30", event_type="EXIT", side="CE", action="HOLD", price=99.0),
        ]
        intents_df = build_order_intents(decisions)["intents"]
        first_intent_id = str(intents_df.iloc[0]["intent_id"])
        fills = [
            {
                "intent_id": first_intent_id,
                "fill_id": "f1",
                "fill_timestamp": "2024-09-03T09:39:00+05:30",
                "side": "CE",
                "order_kind": "OPEN",
                "fill_price": 102.0,
                "filled_qty": 10.0,
                "return_pct": None,
            }
        ]
        report = run_order_intent_runtime(
            decisions,
            fill_events=fills,
            guard_cfg=GuardConfig(max_unmatched_intent_share=1.0, max_side_mismatch_share=1.0, max_consecutive_losses=99, max_drawdown=1.0),
        )
        self.assertEqual(report["reconciliation"]["matched_intents"], 1)
        self.assertEqual(report["reconciliation"]["unmatched_intents"], 1)
        self.assertEqual(report["reconciliation"]["unmatched_fills"], 0)

    def test_runtime_guard_trigger(self) -> None:
        decisions = [
            _decision_event(ts="2024-09-03T09:41:00+05:30", event_type="EXIT", side="CE", action="HOLD", price=95.0, symbol="S1"),
            _decision_event(ts="2024-09-03T09:42:00+05:30", event_type="EXIT", side="CE", action="HOLD", price=90.0, symbol="S2"),
            _decision_event(ts="2024-09-03T09:43:00+05:30", event_type="EXIT", side="CE", action="HOLD", price=85.0, symbol="S3"),
        ]
        intents_df = build_order_intents(decisions)["intents"]
        fills = []
        for i, row in intents_df.reset_index(drop=True).iterrows():
            fills.append(
                {
                    "intent_id": str(row["intent_id"]),
                    "fill_id": f"fx{i+1}",
                    "fill_timestamp": str(row["timestamp"]),
                    "side": "CE",
                    "order_kind": "CLOSE",
                    "fill_price": 90.0 - i,
                    "filled_qty": 10.0,
                    "return_pct": -0.12,
                }
            )
        report = run_order_intent_runtime(
            decisions,
            fill_events=fills,
            guard_cfg=GuardConfig(max_unmatched_intent_share=0.5, max_side_mismatch_share=0.5, max_consecutive_losses=2, max_drawdown=0.2),
        )
        self.assertTrue(report["runtime_guards"]["kill_switch"])
        alert_types = {a["type"] for a in report["runtime_guards"]["alerts"]}
        self.assertIn("consecutive_losses", alert_types)
        self.assertIn("drawdown", alert_types)


if __name__ == "__main__":
    unittest.main()
