import argparse
import asyncio
import glob
import json
import math
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from harness import Harness, RunResult, TestCase, MAX_COMPLETION_TOKENS

BENIGN = "Benign Control"


def _pct(value: float | None) -> str:
    return f"{value}%" if value is not None else "N/A"


def _r1(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None

def load_all_payloads(payloads_dir: str) -> list[tuple[TestCase, str]]:
    suite = []
    for filepath in sorted(glob.glob(os.path.join(payloads_dir, "*.json"))):
        with open(filepath, "r", encoding="utf-8") as file:
            tests = [TestCase(**item) for item in json.load(file)]
            suite.extend((test, test.category) for test in tests)
            ids = [test.id for test, _ in suite]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"Duplicate payload IDs: {dupes}")
            return suite
    return suite


def parse_args():
    parser = argparse.ArgumentParser(description="Run the LLM security evaluation suite.")
    parser.add_argument("--model", default="openai/gpt-oss-20b")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--mode", choices=["baseline", "hardened", "both", "ablation"], default="hardened")
    parser.add_argument("--runs-per-test", type=int, default=3)
    parser.add_argument("--max-concurrency", type=int, default=2)
    return parser.parse_args()


def get_git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("ascii").strip()
    except Exception:
        return "unknown"


def wilson_interval(passed: int, n: int) -> tuple[float, float]:
    """Calculates statistical confidence interval for success rate"""
    if n == 0:
        return 0.0, 0.0
    z = 1.96
    p = passed / n
    den = 1 + z**2 / n
    adj = z**2 / (2 * n)
    err = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    return (p + adj - err) / den * 100, (p + adj + err) / den * 100


def _score(results: list[RunResult]) -> dict[str, Any]:
    usable = [result for result in results if result.status not in ("ERROR", "INCONCLUSIVE")]
    passed = sum(result.passed for result in usable)
    n = len(usable)
    score = passed / n * 100 if n > 0 else None
    lower, upper = wilson_interval(passed, n)
    return {
        "passed": passed,
        "denominator": n,
        "score": score,
        "lower": lower,
        "upper": upper,
        "inconclusive": sum(r.status == "INCONCLUSIVE" for r in results),
        "errors": sum(r.status == "ERROR" for r in results),
    }


def export_reports(
    results: list[RunResult], model: str, output_dir: str, generated_at: datetime,
    args: argparse.Namespace | None = None
) -> Path:
    """Aggregates evaluation results into structured JSON and Markdown reports"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.strftime("%Y_%m_%dT%H_%M_%SZ")
    report_base = output_path / f"evaluation_report_{timestamp}"
    modes = sorted({result.mode for result in results})
    categories = sorted({result.category for result in results})
    summaries: dict[str, Any] = {}

    for mode in modes:
        mode_results = [r for r in results if r.mode == mode]
        s = _score([r for r in mode_results if r.category != BENIGN])
        u = _score([r for r in mode_results if r.category == BENIGN])
        summaries[mode] = {
            "passed": s["passed"], "denominator": s["denominator"],
            "errors": s["errors"], "inconclusive": s["inconclusive"],
            "resilience_score": _r1(s["score"]),
            "interval": [round(s["lower"], 1), round(s["upper"], 1)] if s["score"] is not None else None,
            "utility_score": _r1(u["score"]),
            "utility_passed": u["passed"], "utility_denominator": u["denominator"],
        }
    base = summaries.get("baseline", {}).get("resilience_score")
    gaps = {
        m: (_r1(summaries[m]["resilience_score"] - base)
            if base is not None and summaries[m]["resilience_score"] is not None else None)
        for m in modes if m != "baseline" and "baseline" in summaries
    }
    if gaps:
        summaries["gaps_vs_baseline"] = gaps
    if "hardened" in gaps:
        summaries["gap"] = gaps["hardened"]
    cat_summaries = []

    grouped: dict[tuple[str, str, str], list[RunResult]] = defaultdict(list)
    for result in results:
        grouped[(result.mode, result.test_id, result.name)].append(result)
    test_summaries = []
    for (mode, test_id, name), test_results in grouped.items():
        s = _score(test_results)
        test_summaries.append({
            "mode": mode, "test_id": test_id, "name": name,
            "pass_rate": f"{s['passed']}/{s['denominator']}" if s['denominator'] > 0 else "N/A",
            "resilience_score": round(s["score"], 1) if s["score"] is not None else None,
        })

    metadata = {
        "model": model, "git_sha": get_git_sha(),
        "temperature": 0.3 if args and args.runs_per_test > 1 else 0.0,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "runs_per_test": args.runs_per_test if args else None,
        "mode": args.mode if args else None,
        "ci_note": "Runs of one test are correlated; intervals are optimistic.",
    }

    report = {
        "generated_at": generated_at.isoformat(),
        "metadata": metadata,
        "summary": summaries,
        "category_summaries": cat_summaries,
        "test_summaries": test_summaries,
        "results": [r.model_dump() for r in results],
    }
    report_base.with_suffix(".json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# LLM Security Evaluation",
        "", f"- **Generated:** {report['generated_at']}",
        f"- **Model:** `{model}`",
        f"- **Git SHA:** `{metadata['git_sha']}`",
        f"- **Temperature:** {metadata['temperature']}",
        "",
        "## Scores",
        "", "| Mode | Passed | Denominator | Inconclusive | Errors | Resilience | 95% CI | Utility |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in modes:
        sm = summaries[mode]
        ci = f"[{sm['interval'][0]}%, {sm['interval'][1]}%]" if sm["interval"] else "N/A"
        lines.append(
            f"| {mode} | {sm['passed']} | {sm['denominator']} | {sm['inconclusive']} | "
            f"{sm['errors']} | {_pct(sm['resilience_score'])} | {ci} | "
            f"{_pct(sm['utility_score'])} ({sm['utility_passed']}/{sm['utility_denominator']}) |"
        )
    for m, g in summaries.get("gaps_vs_baseline", {}).items():
        lines.append(f"| {m} - baseline gap | | | | | {_pct(g)} | | |")

    
    lines.extend(["", "## Per-category scores", "", "| Mode | Category | Passed | Denominator | Resilience score |",
                  "|---|---|---:|---:|---:|"])
    for cs in cat_summaries:
        lines.append(
            f"| {cs['mode']} | {cs['category']} | {cs['passed']} | {cs['denominator']} | "
            f"{cs['resilience_score'] if cs['resilience_score'] is not None else 'N/A'}% |"
        )

    lines.extend(["", "## Per-test pass rates", "", "| Mode | ID | Test | Pass rate |",
                  "|---|---|---|---|"])
    lines.extend(
        f"| {item['mode']} | {item['test_id']} | {item['name']} | {item['pass_rate']} |"
        for item in test_summaries
    )
    lines.extend(["", "## Runs", "", "| ID | Mode | Run | Result | Trigger | Finish Reason | Tool URLs |",
                  "|---|---|---:|---|---|---|---|"])
    for result in results:
        lines.append(
            f"| {result.test_id} | {result.mode} | {result.run_number} | "
            f"{result.status} | {result.triggered_indicator} | {result.finish_reason or '-'} | {', '.join(result.tool_urls)} |"
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
    report_base = export_reports(results, args.model, args.output_dir, datetime.now(timezone.utc), args)
    console.print(f"[green]Evaluation reports written:[/green] {report_base.with_suffix('.json')} "
                  f"and {report_base.with_suffix('.md')}")


if __name__ == "__main__":
    asyncio.run(main())
