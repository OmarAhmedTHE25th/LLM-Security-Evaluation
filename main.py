import os
import glob
import json
import asyncio
import argparse
from datetime import datetime, timezone
from pathlib import Path
from rich.console import Console
from rich.table import Table
from harness import Harness, TestCase


def load_all_payloads(payloads_dir: str):
    suite = []
    json_files = glob.glob(os.path.join(payloads_dir, "*.json"))

    for filepath in json_files:
        # Category derived from filename (e.g., canary_leak -> Canary Leak)
        category = os.path.splitext(os.path.basename(filepath))[0].replace("_", " ").title()
        with open(filepath, "r", encoding="utf-8") as f:
            items = json.load(f)
            for item in items:
                suite.append((TestCase(**item), category))
    return suite


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the LLM adversarial security test suite."
    )
    parser.add_argument(
        "--model",
        default="openai/gpt-oss-20b",
        help="Groq model identifier to test (default: openai/gpt-oss-20b)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for timestamped JSON and Markdown audit reports.",
    )
    return parser.parse_args()


def export_reports(results, model: str, output_dir: str, generated_at: datetime):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.strftime("%Y_%m_%dT%H_%M_%SZ")
    report_base = output_path / f"audit_report_{timestamp}"
    total = len(results)
    passed = sum(result.passed for result in results)
    score = (passed / total * 100) if total else 0.0

    report = {
        "generated_at": generated_at.isoformat(),
        "model": model,
        "summary": {
            "passed": passed,
            "total": total,
            "resilience_score": round(score, 1),
        },
        "results": [
            result.model_dump() if hasattr(result, "model_dump") else result.dict()
            for result in results
        ],
    }
    with report_base.with_suffix(".json").open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)

    lines = [
        "# LLM Adversarial Security Audit",
        "",
        f"- **Generated:** {report['generated_at']}",
        f"- **Model:** `{model}`",
        f"- **Resilience score:** {score:.1f}% ({passed}/{total})",
        "",
        "| ID | Category | Test | Result | Triggered indicator |",
        "|---|---|---|---|---|",
    ]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(
            f"| {result.test_id} | {result.category} | {result.name} | "
            f"{status} | {result.triggered_indicator} |"
        )
    lines.extend(["", "## Raw responses", ""])
    for result in results:
        lines.extend(
            [
                f"### {result.test_id}: {result.name}",
                "",
                "```text",
                result.raw_response,
                "```",
                "",
            ]
        )
    report_base.with_suffix(".md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    return report_base


async def main(args=None):
    args = args or parse_args()
    console = Console()
    console.print("\n[bold cyan]=== LLM Adversarial Security Test Suite ===[/bold cyan]\n")

    suite = load_all_payloads("payloads")
    if not suite:
        console.print("[red]No payload files found in payloads/ directory.[/red]")
        return

    harness = Harness(model=args.model)
    results = await harness.run_suite(suite)

    table = Table(show_header=True, header_style="bold white on blue")
    table.add_column("ID", width=12)
    table.add_column("Category", width=18)
    table.add_column("Test Name", width=30)
    table.add_column("Result", justify="center", width=10)
    table.add_column("Leak / Triggered Flag", width=32)

    passed = 0
    for res in results:
        if res.passed:
            status = "[green]PASS[/green]"
            passed += 1
        else:
            status = "[bold red]FAIL[/bold red]"

        table.add_row(
            res.test_id,
            res.category,
            res.name,
            status,
            res.triggered_indicator
        )

    console.print(table)
    total = len(results)
    score = (passed / total * 100) if total else 0.0
    console.print(
        f"\n[bold]Model:[/bold] {args.model} | [bold]Passed:[/bold] {passed}/{total} "
        f"| [bold]Resilience Score:[/bold] {score:.1f}%\n"
    )
    report_base = export_reports(
        results, args.model, args.output_dir, datetime.now(timezone.utc)
    )
    console.print(
        f"[bold green]Audit reports written:[/bold green] "
        f"{report_base.with_suffix('.json')} and {report_base.with_suffix('.md')}"
    )


if __name__ == "__main__":
    asyncio.run(main())