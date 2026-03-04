# GBI Analytics Agent

![Python Version](https://img.shields.io/badge/python-3.12-blue.svg)
![uv](https://img.shields.io/badge/uv-managed-430f8e.svg)
![Gradio](https://img.shields.io/badge/gradio-6.1.0-orange.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## Overview

GBI Analytics Agent is a production-ready Business Intelligence system for **Global Bike Inc. (AdventureBikes)**. It converts plain-English questions into SQL, executes them against a Microsoft SQL Server data mart, and automatically returns an interactive Altair visualization plus a plain-language business explanation — all through a Gradio web interface powered by Google Gemini and Google ADK.

## Architecture

The system is composed of a sequential agent pipeline built with **Google ADK** (`google-adk`):

| Step | Component | File | LLM calls |
|------|-----------|------|-----------|
| 1 | `text_to_sql_agent` (LlmAgent) | `bi_agent/agent.py` | 1 |
| 2 | `PythonSQLExecutorAgent` (BaseAgent) | `bi_agent/agent.py` | 0 |
| 3 | `visualization_agent` (LlmAgent) | `bi_agent/agent.py` | 1 |
| 4 | `explanation_agent` (LlmAgent) | `bi_agent/agent.py` | 1 |

**Total: 3 API calls per query.**

Supporting modules:

- `bi_agent/tools.py` — `execute_sql_and_format()`, `get_database_schema()`
- `bi_agent/db_config.py` — `create_db_engine()`, `get_schema_info()`
- `bi_agent/sql_executor.py` — `validate_sql()` (SELECT-only guardrail), `execute_query()`
- `bi_agent/bi_service.py` — `QueryCache` (LRU, 50 entries), `BIService`
- `app.py` — Gradio Blocks UI with pagination, KPI cards, and retry logic
- `bi_agent/prompts/` — Extracted prompt templates (sql, viz, explain)

## Prerequisites

- Python 3.12+
- Microsoft SQL Server with **ODBC Driver 18 for SQL Server**
- Google Gemini API key (free tier: [Google AI Studio](https://aistudio.google.com/))
- `uv` package manager

## Setup Instructions

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd gradio-adk-agent_V2
```

### 2. Install uv (if not installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 3. Install dependencies

```bash
uv sync
```

### 4. Configure environment variables

```bash
cp bi_agent/.env.example bi_agent/.env
```

Open `bi_agent/.env` and fill in your credentials:

```env
GOOGLE_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash

MSSQL_SERVER=your_server_address
MSSQL_DATABASE=your_database_name
MSSQL_USERNAME=your_username
MSSQL_PASSWORD=your_password
MSSQL_DRIVER=ODBC Driver 18 for SQL Server
TRUST_SERVER_CERTIFICATE=yes
```

### 5. Run the application

**Option A — Gradio interface (recommended):**

```bash
uv run app.py
```

Open: http://127.0.0.1:7860

**Option B — ADK web interface:**

```bash
uv run adk web . --port 8000
```

Open: http://127.0.0.1:8000

Both interfaces use the same `root_agent` pipeline.

## Example Questions

The following questions work directly against the AdventureBikes Sales DataMart:

1. `What are the top 10 products by revenue?`
2. `Show monthly sales trend for 2024`
3. `Which country had the highest total sales in 2023?`
4. `How does actual revenue compare to quota by product category?`
5. `Compare Internet Sales vs Reseller revenue by product category`
6. `What is the gross profit by product category?`

## Project Structure

```
gradio-adk-agent_V2/
├── app.py                       # Gradio UI: run_pipeline_async, process_request, build_chart
├── style.css                    # Dark-theme enterprise CSS for Gradio
├── pyproject.toml               # uv-managed dependencies
├── ARCHITECTURE.md              # 1-page architecture, prompts, safety, evaluation
├── bi_agent/
│   ├── __init__.py              # Package exports (root_agent, root_runner, etc.)
│   ├── agent.py                 # All agent definitions + DB_SCHEMA constant
│   ├── tools.py                 # execute_sql_and_format(), get_database_schema()
│   ├── bi_service.py            # QueryCache (LRU) + BIService class
│   ├── db_config.py             # create_db_engine(), get_schema_info()
│   ├── sql_executor.py          # validate_sql() guardrail, execute_query()
│   ├── .env.example             # Credential template (no real values)
│   └── prompts/
│       ├── sql_prompt.txt       # Text-to-SQL prompt template
│       ├── viz_prompt.txt       # Visualization prompt template
│       └── explain_prompt.txt   # Explanation prompt template
└── evaluation/
    ├── test_cases.json          # 20 test cases with expected SQL/chart/row_count
    ├── run_eval.py              # Evaluation runner (SQL match, chart accuracy, LLM judge)
    └── results/
        ├── raw_results.json     # Machine-readable evaluation output
        └── report.md            # Human-readable evaluation report
```

## Evaluation

Run the full evaluation suite (20 test cases):

```bash
# Full run with LLM-as-judge scoring
uv run python evaluation/run_eval.py

# Skip LLM judge (saves API calls on free tier)
uv run python evaluation/run_eval.py --skip-judge

# Run specific test cases only
uv run python evaluation/run_eval.py --cases 1,2,5
```

Results are saved to `evaluation/results/report.md` and `evaluation/results/raw_results.json`.

**Metrics measured:**
- SQL keyword match score (structural accuracy)
- SQL table match score (semantic accuracy)
- Chart type accuracy (bar/line/pie/scatter)
- Explanation quality (Gemini LLM-as-judge, scale 1–5)
- Response time per pipeline run (milliseconds)

## Database Safety

- Only `SELECT` statements are allowed (`bi_agent/sql_executor.py:validate_sql`)
- Blocked keywords: `DROP, DELETE, UPDATE, INSERT, ALTER, CREATE, TRUNCATE, EXEC, EXECUTE, GRANT, REVOKE, sp_, xp_`
- Query timeout: 30 seconds
- Automatic `TOP 1000` row limit added if not already present
- All credentials loaded from `.env` — never hardcoded

## Troubleshooting

**ODBC Driver not found (macOS):**
```bash
brew install msodbcsql18
```

**Rate limit (429) errors:**
The app automatically retries with exponential backoff: 5s → 15s → 30s → 60s.
On free tier (20 req/day), use `--skip-judge` during evaluation.

**"App name mismatch" warnings on import:**
These are cosmetic ADK warnings and do not affect functionality.
