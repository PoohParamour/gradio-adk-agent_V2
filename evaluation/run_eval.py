"""
Evaluation script for the AdventureBikes BI Agent pipeline.

Measures:
  - SQL accuracy  : keyword match (structural) + table match (semantic)
  - Response time : milliseconds per pipeline run
  - Chart type    : correctness vs expected type from test_cases.json
  - Explanation   : LLM-as-judge score 1-5 (Gemini)

Outputs:
  - Console progress
  - evaluation/results/report.md
  - evaluation/results/raw_results.json
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Project root is one level up from evaluation/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(dotenv_path=ROOT / 'bi_agent' / '.env')

from bi_agent import root_runner
from google.genai import types
from google import genai


# ── Config ────────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash"
TEST_CASES_PATH = Path(__file__).parent / "test_cases.json"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Rate-limit safety: pause between pipeline runs (free tier: 20 req/day)
DELAY_BETWEEN_RUNS = 15  # seconds


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_query_results(raw) -> dict:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict) and list(parsed.keys()) == ['result']:
            inner = parsed['result']
            parsed = json.loads(inner) if isinstance(inner, str) else inner
        return parsed
    except Exception:
        return {'success': False, 'data': [], 'columns': [], 'row_count': 0, 'error': 'parse error'}


def clean_sql(sql: str) -> str:
    sql = sql.strip()
    if sql.startswith("```sql"):
        sql = sql.replace("```sql", "").replace("```", "").strip()
    elif sql.startswith("```"):
        sql = sql.replace("```", "").strip()
    return sql


def detect_chart_type(chart_spec: str) -> str:
    if not chart_spec:
        return "none"
    cs = chart_spec.lower()
    if "mark_line" in cs:
        return "line"
    if "mark_arc" in cs or "mark_pie" in cs:
        return "pie"
    if "mark_point" in cs or "mark_circle" in cs:
        return "scatter"
    if "mark_bar" in cs:
        return "bar"
    return "unknown"


def check_sql_keyword_match(generated_sql: str, expected_keywords: list[str]) -> dict:
    """Check how many expected keywords appear in the generated SQL (case-insensitive)."""
    sql_upper = generated_sql.upper()
    matched = []
    missed = []
    for kw in expected_keywords:
        if kw.upper() in sql_upper:
            matched.append(kw)
        else:
            missed.append(kw)
    score = len(matched) / len(expected_keywords) if expected_keywords else 0.0
    return {"score": round(score, 3), "matched": matched, "missed": missed}


def check_table_match(generated_sql: str, expected_tables: list[str]) -> dict:
    """Check that all expected tables appear in the generated SQL."""
    sql_upper = generated_sql.upper()
    matched = [t for t in expected_tables if t.upper() in sql_upper]
    score = len(matched) / len(expected_tables) if expected_tables else 0.0
    return {"score": round(score, 3), "matched": matched,
            "missed": [t for t in expected_tables if t not in matched]}


async def llm_judge_explanation(question: str, explanation: str, data_preview: str) -> dict:
    """
    Use Gemini as a judge to score explanation quality 1-5.
    Returns {"score": int, "reason": str}
    """
    if not explanation or explanation.strip() == "":
        return {"score": 0, "reason": "No explanation generated"}

    judge_prompt = f"""You are an expert evaluator of business intelligence explanations.

Score the following explanation on a scale of 1 to 5:
  5 = Excellent: concise (2-3 sentences), references specific numbers, clear business language, no jargon
  4 = Good: mostly meets criteria, minor issues
  3 = Acceptable: understandable but vague or missing key numbers
  2 = Poor: too long, too technical, or missing important context
  1 = Very poor: incorrect, empty, or completely off-topic

Question asked: {question}

Data available (preview):
{data_preview}

Explanation to evaluate:
{explanation}

Respond with ONLY a JSON object in this exact format:
{{"score": <integer 1-5>, "reason": "<one sentence justification>"}}"""

    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=judge_prompt,
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return {"score": int(result.get("score", 0)), "reason": result.get("reason", "")}
    except Exception as e:
        return {"score": -1, "reason": f"Judge error: {str(e)}"}


async def run_pipeline(question: str) -> tuple[dict, float]:
    """Run the full pipeline and return (results_dict, elapsed_ms)."""
    start = time.perf_counter()
    session = await root_runner.session_service.create_session(
        user_id='eval', app_name='bi_agent'
    )
    content = types.Content(role='user', parts=[types.Part(text=question)])
    state = {}
    async for event in root_runner.run_async(
        user_id='eval', session_id=session.id, new_message=content
    ):
        if event.actions and event.actions.state_delta:
            for k, v in event.actions.state_delta.items():
                state[k] = v
    elapsed_ms = (time.perf_counter() - start) * 1000
    return state, elapsed_ms


# ── Main evaluation loop ──────────────────────────────────────────────────────

async def evaluate_all(test_cases: list[dict], skip_llm_judge: bool = False) -> list[dict]:
    results = []

    for i, tc in enumerate(test_cases):
        print(f"\n[{i + 1}/{len(test_cases)}] Q{tc['id']}: {tc['question'][:70]}...")

        result_record = {
            "id": tc["id"],
            "question": tc["question"],
            "error": None,
            "response_time_ms": None,
            "sql_generated": None,
            "sql_keyword_score": None,
            "sql_keyword_matched": [],
            "sql_keyword_missed": [],
            "sql_table_score": None,
            "sql_table_matched": [],
            "sql_table_missed": [],
            "chart_type_detected": None,
            "chart_type_expected": tc.get("expected_chart_type"),
            "chart_type_correct": None,
            "explanation": None,
            "llm_judge_score": None,
            "llm_judge_reason": None,
            "row_count": None,
        }

        try:
            state, elapsed_ms = await run_pipeline(tc["question"])
            result_record["response_time_ms"] = round(elapsed_ms, 1)

            # SQL
            sql = clean_sql(state.get("sql_query", ""))
            result_record["sql_generated"] = sql

            kw_check = check_sql_keyword_match(sql, tc.get("expected_sql_keywords", []))
            result_record["sql_keyword_score"] = kw_check["score"]
            result_record["sql_keyword_matched"] = kw_check["matched"]
            result_record["sql_keyword_missed"] = kw_check["missed"]

            tbl_check = check_table_match(sql, tc.get("expected_tables", []))
            result_record["sql_table_score"] = tbl_check["score"]
            result_record["sql_table_matched"] = tbl_check["matched"]
            result_record["sql_table_missed"] = tbl_check["missed"]

            # Query results
            qr = parse_query_results(state.get("query_results", "{}"))
            row_count = qr.get("row_count", 0)
            result_record["row_count"] = row_count

            # Chart
            chart_type = detect_chart_type(state.get("chart_spec", ""))
            result_record["chart_type_detected"] = chart_type
            result_record["chart_type_correct"] = (
                chart_type == tc.get("expected_chart_type")
            )

            # Explanation + LLM judge
            explanation = state.get("explanation_text", "")
            result_record["explanation"] = explanation

            if not skip_llm_judge and explanation:
                data_preview = json.dumps(qr.get("data", [])[:5], indent=2)
                judge = await llm_judge_explanation(
                    tc["question"], explanation, data_preview
                )
                result_record["llm_judge_score"] = judge["score"]
                result_record["llm_judge_reason"] = judge["reason"]

            status = "OK" if qr.get("success") else "SQL_FAIL"
            print(f"  Status: {status} | Time: {elapsed_ms:.0f}ms | Rows: {row_count} | "
                  f"SQL kw: {kw_check['score']:.0%} | Chart: {chart_type} "
                  f"({'correct' if result_record['chart_type_correct'] else 'WRONG'})")

        except Exception as e:
            result_record["error"] = str(e)
            print(f"  ERROR: {e}")

        results.append(result_record)

        if i < len(test_cases) - 1:
            print(f"  Waiting {DELAY_BETWEEN_RUNS}s (rate limit)...")
            await asyncio.sleep(DELAY_BETWEEN_RUNS)

    return results


# ── Report generation ─────────────────────────────────────────────────────────

def generate_report(results: list[dict]) -> str:
    completed = [r for r in results if r["error"] is None]
    errored = [r for r in results if r["error"] is not None]
    total = len(results)

    sql_kw_scores = [r["sql_keyword_score"] for r in completed if r["sql_keyword_score"] is not None]
    sql_tbl_scores = [r["sql_table_score"] for r in completed if r["sql_table_score"] is not None]
    times_ms = [r["response_time_ms"] for r in completed if r["response_time_ms"] is not None]
    chart_correct = [r for r in completed if r["chart_type_correct"] is True]
    judge_scores = [r["llm_judge_score"] for r in completed
                    if r["llm_judge_score"] is not None and r["llm_judge_score"] > 0]

    avg_kw = sum(sql_kw_scores) / len(sql_kw_scores) if sql_kw_scores else 0
    avg_tbl = sum(sql_tbl_scores) / len(sql_tbl_scores) if sql_tbl_scores else 0
    avg_time = sum(times_ms) / len(times_ms) if times_ms else 0
    min_time = min(times_ms) if times_ms else 0
    max_time = max(times_ms) if times_ms else 0
    chart_acc = len(chart_correct) / len(completed) if completed else 0
    avg_judge = sum(judge_scores) / len(judge_scores) if judge_scores else 0

    lines = [
        "# BI Agent Evaluation Report",
        "",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Model:** gemini-2.5-flash  ",
        f"**Test cases:** {total} total, {len(completed)} completed, {len(errored)} errors  ",
        "",
        "---",
        "",
        "## Summary Metrics",
        "",
        "| Metric | Score |",
        "|--------|-------|",
        f"| SQL Keyword Match (avg) | {avg_kw:.1%} |",
        f"| SQL Table Match (avg) | {avg_tbl:.1%} |",
        f"| Chart Type Accuracy | {chart_acc:.1%} ({len(chart_correct)}/{len(completed)}) |",
        f"| Explanation Quality (LLM judge, 1-5) | {avg_judge:.2f} |",
        f"| Avg Response Time | {avg_time:.0f} ms |",
        f"| Min Response Time | {min_time:.0f} ms |",
        f"| Max Response Time | {max_time:.0f} ms |",
        "",
        "---",
        "",
        "## Per-Test Results",
        "",
        "| ID | Question (short) | SQL kw | SQL tbl | Chart | Judge | Time (ms) | Error |",
        "|----|-----------------|--------|---------|-------|-------|-----------|-------|",
    ]

    for r in results:
        q_short = r["question"][:45] + ("..." if len(r["question"]) > 45 else "")
        sql_kw = f"{r['sql_keyword_score']:.0%}" if r['sql_keyword_score'] is not None else "–"
        sql_tbl = f"{r['sql_table_score']:.0%}" if r['sql_table_score'] is not None else "–"
        chart_ok = "OK" if r['chart_type_correct'] else (
            f"WRONG ({r['chart_type_detected']})" if r['chart_type_detected'] else "–"
        )
        judge = str(r['llm_judge_score']) if r['llm_judge_score'] and r['llm_judge_score'] > 0 else "–"
        t = f"{r['response_time_ms']:.0f}" if r['response_time_ms'] else "–"
        err = r['error'][:30] if r['error'] else ""
        lines.append(f"| {r['id']} | {q_short} | {sql_kw} | {sql_tbl} | {chart_ok} | {judge} | {t} | {err} |")

    lines += [
        "",
        "---",
        "",
        "## SQL Details (missed keywords)",
        "",
    ]
    for r in results:
        if r.get("sql_keyword_missed"):
            lines.append(f"**Q{r['id']}** — missed: `{', '.join(r['sql_keyword_missed'])}`  ")
            lines.append(f"SQL: `{(r['sql_generated'] or '')[:120]}`  ")
            lines.append("")

    lines += [
        "",
        "## Explanation Samples",
        "",
    ]
    for r in results[:5]:
        if r.get("explanation"):
            lines.append(f"**Q{r['id']}:** {r['explanation']}  ")
            lines.append(f"*Judge score: {r['llm_judge_score']} — {r['llm_judge_reason']}*  ")
            lines.append("")

    if errored:
        lines += ["", "## Errors", ""]
        for r in errored:
            lines.append(f"- **Q{r['id']}** ({r['question'][:50]}): {r['error']}")

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run BI Agent evaluation")
    parser.add_argument("--skip-judge", action="store_true",
                        help="Skip LLM-as-judge scoring (saves API calls)")
    parser.add_argument("--cases", type=str, default=None,
                        help="Comma-separated test case IDs to run, e.g. '1,2,5'")
    parser.add_argument("--delay", type=int, default=DELAY_BETWEEN_RUNS,
                        help=f"Seconds between runs (default: {DELAY_BETWEEN_RUNS})")
    args = parser.parse_args()

    with open(TEST_CASES_PATH) as f:
        all_cases = json.load(f)

    if args.cases:
        ids = {int(x.strip()) for x in args.cases.split(",")}
        test_cases = [tc for tc in all_cases if tc["id"] in ids]
        print(f"Running {len(test_cases)} selected test cases: {sorted(ids)}")
    else:
        test_cases = all_cases
        print(f"Running all {len(test_cases)} test cases")

    # Override module-level delay if specified
    if args.delay != 15:
        import evaluation.run_eval as _self
        _self.DELAY_BETWEEN_RUNS = args.delay

    print(f"Delay between runs: {DELAY_BETWEEN_RUNS}s")
    print(f"LLM judge: {'disabled' if args.skip_judge else 'enabled'}")
    print("=" * 60)

    results = await evaluate_all(test_cases, skip_llm_judge=args.skip_judge)

    # Save raw JSON
    raw_path = RESULTS_DIR / "raw_results.json"
    with open(raw_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nRaw results saved to: {raw_path}")

    # Generate and save report
    report = generate_report(results)
    report_path = RESULTS_DIR / "report.md"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved to: {report_path}")

    # Print summary
    completed = [r for r in results if r["error"] is None]
    kw_scores = [r["sql_keyword_score"] for r in completed if r["sql_keyword_score"] is not None]
    chart_ok = [r for r in completed if r["chart_type_correct"] is True]
    print("\n" + "=" * 60)
    print("EVALUATION COMPLETE")
    print(f"  Completed: {len(completed)}/{len(results)}")
    if kw_scores:
        print(f"  SQL keyword match (avg): {sum(kw_scores)/len(kw_scores):.1%}")
    if completed:
        print(f"  Chart type accuracy: {len(chart_ok)}/{len(completed)} ({len(chart_ok)/len(completed):.1%})")
    print(f"  Report: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
