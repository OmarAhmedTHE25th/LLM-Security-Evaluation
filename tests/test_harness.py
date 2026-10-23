import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from harness import RunResult
from main import export_reports, load_all_payloads


class HarnessTests(unittest.TestCase):
    def test_all_payloads_are_valid_and_loaded(self):
        suite = load_all_payloads("payloads")
        self.assertEqual(len(suite), 28)
        self.assertTrue(all(test.id and category for test, category in suite))

    def test_export_reports_writes_json_and_markdown(self):
        result = RunResult(
            test_id="TEST-01",
            name="Export smoke test",
            category="Test",
            mode="hardened",
            status="PASS",
            passed=True,
            triggered_indicator="-",
            raw_response="safe response",
        )
        generated_at = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            report_base = export_reports(
                [result], "test-model", directory, generated_at
            )
            json_path = report_base.with_suffix(".json")
            markdown_path = report_base.with_suffix(".md")
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["model"], "test-model")
            self.assertEqual(report["summary"]["resilience_score"], 100.0)
            self.assertIn("Export smoke test", markdown_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
