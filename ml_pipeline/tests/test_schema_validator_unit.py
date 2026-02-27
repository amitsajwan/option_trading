import tempfile
import unittest
from pathlib import Path

from ml_pipeline.schema_validator import build_file_path, discover_available_days, validate_file


def _write_csv(path: Path, header: str, rows: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header}\n{rows}\n", encoding="utf-8")


class SchemaValidatorUnitTests(unittest.TestCase):
    def test_validate_fut_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "fut.csv"
            _write_csv(
                file_path,
                "date,time,symbol,open,high,low,close,oi,volume",
                "2023-06-15,09:15:00,BANKNIFTY-I,44150,44180,44022.4,44040.5,2423850,56228",
            )
            result = validate_file(path=file_path, dataset="fut")
            self.assertTrue(result.ok)
            self.assertEqual(result.rows, 1)
            self.assertEqual(result.errors, [])

    def test_validate_options_missing_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "options.csv"
            _write_csv(
                file_path,
                "date,time,symbol,open,high,low,close,volume",
                "2023-06-15,09:15:00,BANKNIFTY15JUN2337500PE,1,1.1,0.9,1.0,100",
            )
            result = validate_file(path=file_path, dataset="options")
            self.assertFalse(result.ok)
            self.assertTrue(any("Missing required columns" in err for err in result.errors))

    def test_validate_spot_duplicate_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "spot.csv"
            _write_csv(
                file_path,
                "date,time,symbol,open,high,low,close",
                "\n".join(
                    [
                        "2023-06-15,09:15:00,BANKNIFTY,44054.35,44077.45,43932.15,43943.95",
                        "2023-06-15,09:15:00,BANKNIFTY,44054.35,44077.45,43932.15,43943.95",
                    ]
                ),
            )
            result = validate_file(path=file_path, dataset="spot")
            self.assertFalse(result.ok)
            self.assertTrue(any("Duplicate primary key rows" in err for err in result.errors))

    def test_build_file_path_shapes(self) -> None:
        base = Path(r"C:\archive\banknifty_data")
        fut = build_file_path(base, "fut", "2023-06-15")
        options = build_file_path(base, "options", "2023-06-15")
        spot = build_file_path(base, "spot", "2023-06-15")
        self.assertTrue(str(fut).endswith(r"banknifty_fut\2023\6\banknifty_fut_15_06_2023.csv"))
        self.assertTrue(str(options).endswith(r"banknifty_options\2023\6\banknifty_options_15_06_2023.csv"))
        self.assertTrue(str(spot).endswith(r"banknifty_spot\2023\6\banknifty_spot15_06_2023.csv"))

    def test_discover_available_days_common_intersection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "banknifty_fut" / "2023" / "6").mkdir(parents=True, exist_ok=True)
            (base / "banknifty_options" / "2023" / "6").mkdir(parents=True, exist_ok=True)
            (base / "banknifty_spot" / "2023" / "6").mkdir(parents=True, exist_ok=True)
            # common day
            (base / "banknifty_fut" / "2023" / "6" / "banknifty_fut_15_06_2023.csv").write_text("x", encoding="utf-8")
            (base / "banknifty_options" / "2023" / "6" / "banknifty_options_15_06_2023.csv").write_text("x", encoding="utf-8")
            (base / "banknifty_spot" / "2023" / "6" / "banknifty_spot15_06_2023.csv").write_text("x", encoding="utf-8")
            # fut-only day (should not appear)
            (base / "banknifty_fut" / "2023" / "6" / "banknifty_fut_16_06_2023.csv").write_text("x", encoding="utf-8")

            out = discover_available_days(base)
            self.assertEqual(out, ["2023-06-15"])


if __name__ == "__main__":
    unittest.main()
