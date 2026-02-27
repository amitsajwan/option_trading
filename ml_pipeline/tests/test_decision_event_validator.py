import json
import tempfile
import unittest
from pathlib import Path

from ml_pipeline.decision_event_validator import validate_event, validate_jsonl


def _valid_common() -> dict:
    return {
        "generated_at": "2026-02-22T10:00:00+00:00",
        "timestamp": "2026-02-22T10:00:00+00:00",
        "trade_date": "2026-02-22",
        "mode": "dual",
        "ce_prob": 0.65,
        "pe_prob": 0.22,
        "ce_threshold": 0.5,
        "pe_threshold": 0.7,
        "action": "BUY_CE",
        "confidence": 0.65,
    }


class DecisionEventValidatorTests(unittest.TestCase):
    def test_validate_event_ok_for_exit_stream(self) -> None:
        event = _valid_common()
        event.update(
            {
                "event_type": "MANAGE",
                "event_reason": "hold",
                "held_minutes": 2,
                "position": {
                    "side": "CE",
                    "entry_timestamp": "2026-02-22T09:58:00+00:00",
                    "entry_confidence": 0.72,
                },
            }
        )
        self.assertEqual(validate_event(event), [])

    def test_validate_event_rejects_invalid_action(self) -> None:
        event = _valid_common()
        event["action"] = "BUY_X"
        errors = validate_event(event)
        self.assertIn("invalid_action", errors)

    def test_validate_event_rejects_missing_position_for_exit(self) -> None:
        event = _valid_common()
        event.update({"event_type": "EXIT", "event_reason": "time_stop", "held_minutes": 3})
        errors = validate_event(event)
        self.assertIn("invalid_position", errors)

    def test_validate_jsonl_report(self) -> None:
        ok = _valid_common()
        ok.update(
            {
                "event_type": "ENTRY",
                "event_reason": "signal_entry",
                "position": {
                    "side": "PE",
                    "entry_timestamp": "2026-02-22T10:00:00+00:00",
                    "entry_confidence": 0.81,
                },
            }
        )
        bad = _valid_common()
        bad["mode"] = "invalid_mode"

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(ok),
                        json.dumps(bad),
                        "not_json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = validate_jsonl(path)
            self.assertEqual(report["rows_total"], 3)
            self.assertEqual(report["valid_rows"], 1)
            self.assertEqual(report["invalid_rows"], 2)
            self.assertEqual(report["status"], "fail")
            self.assertIn("invalid_mode", report["error_type_counts"])
            self.assertIn("invalid_json", report["error_type_counts"])


if __name__ == "__main__":
    unittest.main()
