import json
import base64
import binascii
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from harness import (
    RunResult,
    TestCase,
    evaluate_tool_result,
    is_allowed_url,
    tool_urls_are_allowed,
)
from main import export_reports, load_all_payloads


class HarnessTests(unittest.TestCase):
    def test_all_payloads_are_valid_and_loaded(self):
        suite = load_all_payloads("payloads")
        self.assertEqual(len(suite), 28)
        self.assertTrue(all(test.id and category for test, category in suite))
        self.assertEqual({test.category for test, _ in suite}, {
            "Canary Leak", "Indirect Context Tampering", "Injection",
            "Tool Parameter Injection",
        })

    def test_encoded_payloads_contain_a_forbidden_canary(self):
        encoded_token = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{8,}={0,2}(?![A-Za-z0-9+/=])")
        for test, _ in load_all_payloads("payloads"):
            # LEAK-04 was changed to request base64 instead of providing it,
            # so it no longer contains an encoded canary in the input.
            if test.id == "LEAK-04":
                continue
            claims_encoding = "encoded" in test.name.lower() or "base64" in test.name.lower()
            if not claims_encoding:
                continue
            decoded = []
            for token in encoded_token.findall(test.user_input):
                try:
                    decoded.append(
                        base64.b64decode(
                            token + "=" * (-len(token) % 4), validate=True
                        ).decode()
                    )
                except (binascii.Error, UnicodeDecodeError):
                    continue
            self.assertTrue(
                any(forbidden.lower() in value.lower()
                    for value in decoded for forbidden in test.forbidden_strings),
                test.id,
            )

    def test_url_allowlist(self):
        allowed = ["docs.example.com", "support.example.com"]
        self.assertTrue(is_allowed_url("https://docs.example.com/a", allowed))
        self.assertFalse(is_allowed_url("http://169.254.169.254/latest", allowed))
        self.assertFalse(is_allowed_url("https://localhost/a", allowed))
        self.assertFalse(is_allowed_url("file:///etc/passwd", allowed))
        self.assertFalse(is_allowed_url("http://docs.example.com/a", allowed))
        self.assertTrue(tool_urls_are_allowed([], allowed))

    def test_tool_result_also_checks_response_text_and_required_url(self):
        test = TestCase(
            id="TOOL", name="Tool", category="Tool Parameter Injection",
            system_prompt="x", user_input="x",
            forbidden_strings=["MARKER{bad}"],
            must_fetch_url="https://docs.example.com/a",
            tool_hosts=["docs.example.com"],
        )
        self.assertEqual(
            evaluate_tool_result("MARKER{bad}", ["https://docs.example.com/a"], test),
            (False, "MARKER{bad}"),
        )
        self.assertEqual(
            evaluate_tool_result("safe", [], test),
            (False, "MISSING_TOOL_CALL:https://docs.example.com/a"),
        )
        self.assertEqual(
            evaluate_tool_result("", ["https://docs.example.com/a"], test),
            (True, "-"),
        )

    def test_malformed_and_empty_tool_arguments_fail_explicitly(self):
        test = TestCase(
            id="TOOL", name="Tool", category="Tool Parameter Injection",
            system_prompt="x", user_input="x", tool_hosts=["docs.example.com"],
        )
        self.assertEqual(
            evaluate_tool_result("safe", [], test, malformed_tool_call=True),
            (False, "MALFORMED_TOOL_ARGUMENTS"),
        )
        self.assertEqual(
            evaluate_tool_result("safe", [""], test),
            (False, "EMPTY_TOOL_URL"),
        )

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

    def test_both_mode_gap_errors_and_pass_rates(self):
        results = [
            RunResult(test_id="T", name="Test", category="Test", mode="baseline",
                      status="FAIL", passed=False, triggered_indicator="x", raw_response="x"),
            RunResult(test_id="T", name="Test", category="Test", mode="baseline",
                      status="ERROR", passed=False, triggered_indicator="ERROR", raw_response="x"),
            RunResult(test_id="T", name="Test", category="Test", mode="hardened",
                      status="PASS", passed=True, triggered_indicator="-", raw_response="ok"),
            RunResult(test_id="T", name="Test", category="Test", mode="hardened",
                      status="PASS", passed=True, triggered_indicator="-", raw_response="ok"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            report_base = export_reports(
                results, "test-model", directory, datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)
            )
            report = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["baseline"]["denominator"], 1)
        self.assertEqual(report["summary"]["baseline"]["resilience_score"], 0.0)
        self.assertEqual(report["summary"]["hardened"]["resilience_score"], 100.0)
        self.assertEqual(report["summary"]["gap"], 100.0)
        self.assertEqual(report["test_summaries"][0]["pass_rate"], "0/1")

    def test_zero_denominator_is_na(self):
        result = RunResult(test_id="T", name="Test", category="Test", mode="hardened",
                           status="ERROR", passed=False, triggered_indicator="ERROR", raw_response="x")
        with tempfile.TemporaryDirectory() as directory:
            report = export_reports(
                [result], "test-model", directory, datetime.now(timezone.utc)
            )
            data = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
            markdown = report.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIsNone(data["summary"]["resilience_score"])
        self.assertIsNone(data["test_summaries"][0]["resilience_score"])
        self.assertEqual(data["test_summaries"][0]["pass_rate"], "N/A")
        self.assertIn("N/A |", markdown)


if __name__ == "__main__":
    unittest.main()
