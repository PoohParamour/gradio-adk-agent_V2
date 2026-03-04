# Architecture Overview

## System Architecture

```
User (Gradio UI — app.py)
        │
        │  Natural language question
        ▼
┌─────────────────────────────────────────────────┐
│  root_agent  (SequentialAgent — agent.py)       │
│                                                 │
│  ① text_to_sql_agent  (LlmAgent)               │
│     · Model: gemini-2.5-flash                   │
│     · Input: user question + embedded DB_SCHEMA │
│     · Output → session state: sql_query         │
│                                                 │
│  ② PythonSQLExecutorAgent  (BaseAgent, 0 LLM)  │
│     · Calls execute_sql_and_format() tool       │
│     · validate_sql() guardrail runs first       │
│     · Connects via pyodbc / SQLAlchemy engine   │
│     · Output → session state: query_results,    │
│                               formatted_data    │
│                                                 │
│  ③ insight_pipeline  (SequentialAgent)          │
│     │                                           │
│     ├─ visualization_agent  (LlmAgent)          │
│     │   · Input: formatted_data (50 rows max)   │
│     │   · Output → session state: chart_spec    │
│     │                                           │
│     └─ explanation_agent   (LlmAgent)           │
│         · Input: formatted_data                 │
│         · Output → session state: explanation_text│
└─────────────────────────────────────────────────┘
        │
        ▼
  Gradio renders: SQL tab · Data Table · Chart · Explanation
        │
        ▼
  QueryCache (LRU, 50 entries) in bi_service.py
```

**Total LLM API calls per query: 3.** The SQL executor is pure Python (zero LLM cost).

## Component Descriptions

**`app.py`** — Gradio Blocks UI. `process_request()` orchestrates the pipeline via `run_pipeline_with_retry()` (exponential backoff: 5 → 15 → 30 → 60 s on 429 errors). `build_chart()` calls `validate_and_fix_chart_code()` before `exec()`-ing Altair code. `get_page()` handles pagination (20 rows/page). `_query_cache` (imported from `bi_service.py`) short-circuits repeated questions.

**`bi_agent/agent.py`** — All agent definitions. `DB_SCHEMA` is embedded as a constant string (avoids an extra `get_database_schema` API call per query). `validate_and_fix_chart_code()` is a post-processing step that detects and corrects "Ghost Bar" patterns (labels mistakenly derived from a mark layer instead of the base chart).

**`bi_agent/sql_executor.py`** — `validate_sql()` strips comments, normalizes to uppercase, then asserts the query starts with `SELECT` and contains none of the blocked keywords. `execute_query()` wraps `pd.read_sql()` with a 30-second timeout and auto-injects `TOP 1000` when no row limit is present.

**`bi_agent/db_config.py`** — Builds a URL-encoded pyodbc connection string and returns a SQLAlchemy `Engine`. `TrustServerCertificate=yes` is included for the GBI SQL Server 2019 endpoint.

## Prompt Strategy

All prompts are defined inline in `bi_agent/agent.py` using XML tags and are also available as templates in `bi_agent/prompts/`.

**text_to_sql_agent** — Uses a `<role>` block (Senior Database Engineer, 10 yr BI), a full `<database_schema>` block (embedded at import time), a `<rules>` block (SELECT-only, no markdown fences, bracket notation for space columns, `TOP N` not `LIMIT`), a `<query_construction_guide>` with keyword-to-SQL mappings, and five `<examples>`. The model is instructed to output *only* the raw SQL string.

**visualization_agent** — Uses a `<CRITICAL_BASE_CHART_PATTERN>` block that mandates a three-step Altair pattern: define `base` with no mark → derive `bars = base.mark_bar(...)` → derive `labels = base.mark_text(...)` → combine with `+`. This prevents invisible ("Ghost") chart marks. Styling rules select donut charts for ≤3 categories and horizontal bar charts for rankings.

**explanation_agent** — Two-sentence maximum, always cites specific numbers from `{formatted_data}`, avoids technical language ("SQL", "query", "table"), and bolds the single most important figure.

## Safety Measures

`bi_agent/sql_executor.py` — `validate_sql()` (lines 22–61):
- Strips SQL comments (`--` and `/* */`) before analysis
- Requires query to start with `SELECT`
- Blocks these keywords via `\b` word-boundary regex: `DROP, DELETE, UPDATE, INSERT, ALTER, CREATE, TRUNCATE, EXEC, EXECUTE, GRANT, REVOKE, sp_, xp_`
- Rejects multiple statements (more than one semicolon)

`bi_agent/tools.py` — `execute_sql_and_format()` calls `execute_query()` which calls `validate_sql()` before any database connection is attempted.

All credentials are loaded from `bi_agent/.env` via `python-dotenv`. The `.env` file is listed in `.gitignore` and is never committed.

## Evaluation Procedure

**Test suite:** `evaluation/test_cases.json` — 20 cases covering revenue aggregation, time-series trends, quota comparison, product dimension queries, and multi-filter scenarios. Each case specifies: `question`, `expected_sql_keywords` (list), `expected_tables` (list), `expected_chart_type` (bar/line/pie), `expected_row_count`.

**Runner:** `evaluation/run_eval.py` — `evaluate_all()` iterates every test case, calls `run_pipeline()` (the full `root_runner` ADK pipeline), then measures:

1. **SQL keyword score** — fraction of `expected_sql_keywords` found (case-insensitive) in the generated SQL.
2. **SQL table score** — fraction of `expected_tables` present in the generated SQL.
3. **Chart type accuracy** — `detect_chart_type()` inspects `chart_spec` for `mark_bar`, `mark_line`, `mark_arc`, `mark_point`; compared against `expected_chart_type`.
4. **Explanation quality** — Gemini LLM-as-judge scores the explanation 1–5 based on conciseness, number citation, and business clarity.
5. **Response time** — wall-clock milliseconds for the full pipeline.

A 15-second delay is inserted between runs to respect the free-tier rate limit (20 req/day). Results are written to `evaluation/results/raw_results.json` (machine-readable) and `evaluation/results/report.md` (human-readable markdown table). Run with `--skip-judge` to omit LLM scoring, or `--cases 1,2,5` to evaluate a subset.
