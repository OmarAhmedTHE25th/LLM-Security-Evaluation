import argparse
import asyncio
import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from harness import Harness, RunResult, TestCase


def load_all_payloads(payloads_dir: str) -> list[tuple[TestCase, str]]:
    suite = []
    for filepath in sorted(glob.glob(os.path.join(payloads_dir, "*.json"))):
        with open(filepath, "r", encoding="utf-8") as file:
            tests = [TestCase(**item) for item in json.load(file)]
            suite.extend((test, test.category) for test in tests)
    return suite


def parse_args():
    parser = argparse.ArgumentParser(description="Run the LLM security evaluation suite.")
    parser.add_argument("--model", default="openai/gpt-oss-20b")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--mode", choices=["baseline", "hardened", "both"], default="hardened")
    parser.add_argument("--runs-per-test", type=int, default=1)
    parser.add_argument("--max-concurrency", type=int, default=4)
    return parser.parse_args()


def _score(results: list[RunResult]) -> tuple[int, int, float | None]:
    usable = [result for result in results if result.status != "ERROR"]
    passed = sum(result.passed for result in usable)
    return passed, len(usable), passed / len(usable) * 100 if usable else None


def export_reports(
    results: list[RunResult], model: str, output_dir: str, generated_at: datetime
) -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.strftime("%Y_%m_%dT%H_%M_%SZ")
    report_base = output_path / f"evaluation_report_{timestamp}"
    modes = sorted({result.mode for result in results})
    summaries: dict[str, Any] = {}
    for mode in modes:
        passed, denominator, score = _score([r for r in results if r.mode == mode])
        summaries[mode] = {
            "passed": passed, "denominator": denominator,
            "errors": sum(r.status == "ERROR" for r in results if r.mode == mode),
            "resilience_score": round(score, 1) if score is not None else None,
        }
    if len(modes) == 1:
        summaries["passed"] = summaries[modes[0]]["passed"]
        summaries["total"] = summaries[modes[0]]["denominator"]
        summaries["resilience_score"] = summaries[modes[0]]["resilience_score"]
    if "baseline" in summaries and "hardened" in summaries:
        baseline_score = summaries["baseline"]["resilience_score"]
        hardened_score = summaries["hardened"]["resilience_score"]
        summaries["gap"] = (
            round(hardened_score - baseline_score, 1)
            if baseline_score is not None and hardened_score is not None
            else None
        )
    grouped: dict[tuple[str, str, str], list[RunResult]] = defaultdict(list)
    for result in results:
        grouped[(result.mode, result.test_id, result.name)].append(result)
    test_summaries = []
    for (mode, test_id, name), test_results in grouped.items():
        passed, denominator, score = _score(test_results)
        test_summaries.append({
            "mode": mode, "test_id": test_id, "name": name,
            "pass_rate": f"{passed}/{denominator}",
            "resilience_score": round(score, 1) if score is not None else None,
        })
    report = {
        "generated_at": generated_at.isoformat(), "model": model,
        "summary": summaries, "test_summaries": test_summaries,
        "results": [r.model_dump() for r in results],
    }
    report_base.with_suffix(".json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# LLM Security Evaluation",
        "", f"- **Generated:** {report['generated_at']}", f"- **Model:** `{model}`", "",
        "## Scores",
        "", "| Mode | Passed | Denominator | Errors | Resilience score |",
        "|---|---:|---:|---:|---:|",
    ]
    for mode in modes:
        summary = summaries[mode]
        lines.append(
            f"| {mode} | {summary['passed']} | {summary['denominator']} | "
            f"{summary['errors']} | "
            f"{summary['resilience_score'] if summary['resilience_score'] is not None else 'N/A'}% |"
        )
    if "gap" in summaries:
        summary = summaries["gap"]
        lines.append(
            f"| hardened - baseline gap | | | | "
            f"{summary if summary is not None else 'N/A'}% |"
        )
    lines.extend(["", "## Per-test pass rates", "", "| Mode | ID | Test | Pass rate |",
                  "|---|---|---|---|"])
    lines.extend(
        f"| {item['mode']} | {item['test_id']} | {item['name']} | {item['pass_rate']} |"
        for item in test_summaries
    )
    lines.extend(["", "## Runs", "", "| ID | Mode | Run | Result | Trigger | Tool URLs |",
                  "|---|---|---:|---|---|---|"])
    for result in results:
        lines.append(
            f"| {result.test_id} | {result.mode} | {result.run_number} | "
            f"{result.status} | {result.triggered_indicator} | {', '.join(result.tool_urls)} |"
        )
    lines.extend(["", "## Raw responses", ""])
    for result in results:
        lines.extend([f"### {result.test_id} ({result.mode}, run {result.run_number})",
                      "", "```text", result.raw_response, "```", ""])
    report_base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return report_base


async def main(args=None):
    args = args or parse_args()
    console = Console()
    suite = load_all_payloads("payloads")
    if not suite:
        console.print("[red]No payload files found in payloads/ directory.[/red]")
        return
    harness = Harness(
        model=args.model, max_concurrency=args.max_concurrency,
        mode=args.mode, runs_per_test=args.runs_per_test,
    )
    results = await harness.run_suite(suite)
    table = Table(show_header=True, header_style="bold white on blue")
    for column in ("ID", "Mode", "Run", "Test", "Result", "Trigger"):
        table.add_column(column)
    for result in results:
        style = "green" if result.status == "PASS" else "red" if result.status == "FAIL" else "yellow"
        table.add_row(result.test_id, result.mode, str(result.run_number), result.name,
                      f"[{style}]{result.status}[/{style}]", result.triggered_indicator)
    console.print(table)
    report_base = export_reports(results, args.model, args.output_dir, datetime.now(timezone.utc))
    console.print(f"[green]Evaluation reports written:[/green] {report_base.with_suffix('.json')} "
                  f"and {report_base.with_suffix('.md')}")


if __name__ == "__main__":
    asyncio.run(main())
