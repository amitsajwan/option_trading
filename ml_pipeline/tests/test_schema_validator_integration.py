import os
import unittest

from ml_pipeline.schema_validator import (
    DEFAULT_REPRESENTATIVE_DAYS,
    resolve_archive_base,
    validate_days,
)


class SchemaValidatorIntegrationTests(unittest.TestCase):
    def test_representative_days_archive_schema(self) -> None:
        explicit = os.getenv("LOCAL_HISTORICAL_BASE")
        base = resolve_archive_base(explicit_base=explicit)
        if base is None:
            self.skipTest("Archive base path not found")

        report = validate_days(base_path=base, days=DEFAULT_REPRESENTATIVE_DAYS)
        report_dict = report.to_dict()
        summary = report_dict["summary"]
        if summary["fail_count"] != 0:
            details = []
            for day in report.results:
                for item in day.files:
                    if item.errors:
                        details.append(f"{day.date} {item.dataset}: {'; '.join(item.errors)}")
            self.fail("\n".join(details))
        self.assertEqual(summary["fail_count"], 0)


if __name__ == "__main__":
    unittest.main()

