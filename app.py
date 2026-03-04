"""AdventureBikes BI Agent — 2026 Premium Edition."""

import time
import base64
import gradio as gr
import asyncio
import os
import json
import pandas as pd
import altair as alt
from pathlib import Path
from dotenv import load_dotenv
from google.genai import types

from bi_agent import root_runner, validate_and_fix_chart_code
from bi_agent.bi_service import _query_cache

load_dotenv(dotenv_path='bi_agent/.env')

_RETRY_DELAYS = [5, 15, 30, 60]
PAGE_SIZE = 20

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline_async(user_question: str) -> dict:
    session = await root_runner.session_service.create_session(
        user_id='user', app_name='bi_agent'
    )
    content = types.Content(role='user', parts=[types.Part(text=user_question)])
    results = {}
    async for event in root_runner.run_async(
        user_id='user', session_id=session.id, new_message=content
    ):
        if event.actions and event.actions.state_delta:
            for k, v in event.actions.state_delta.items():
                results[k] = v
    return results


async def run_pipeline_with_retry(user_question: str) -> dict:
    last_error: Exception | None = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay > 0:
            print(f"[BI Agent] Rate limit — attempt {attempt}/{len(_RETRY_DELAYS)}, retrying in {delay}s...")
            await asyncio.sleep(delay)
        try:
            return await run_pipeline_async(user_question)
        except Exception as e:
            err = str(e)
            if ('429' in err or 'RESOURCE_EXHAUSTED' in err
                    or 'ResourceExhausted' in type(e).__name__) and attempt < len(_RETRY_DELAYS):
                last_error = e
                continue
            raise
    raise last_error  # type: ignore[misc]


def parse_query_results(raw) -> dict:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict) and list(parsed.keys()) == ['result']:
            inner = parsed['result']
            parsed = json.loads(inner) if isinstance(inner, str) else inner
        return parsed
    except Exception:
        return {'success': False, 'data': [], 'columns': [], 'row_count': 0,
                'error': 'Failed to parse query results'}


def clean_sql(sql: str) -> str:
    sql = sql.strip()
    for marker in ["```sql", "```"]:
        sql = sql.replace(marker, "")
    return sql.strip()


def clean_code(code: str) -> str:
    code = code.strip()
    for marker in ["```python", "```"]:
        code = code.replace(marker, "")
    return code.strip()


def build_chart(chart_spec: str, df: pd.DataFrame):
    if not chart_spec:
        print("[Chart] chart_spec is empty — visualization_agent returned nothing")
        return None
    try:
        # Pre-process code to fix known issues (like Ghost Bars)
        code = validate_and_fix_chart_code(clean_code(chart_spec))
        
        print(f"[Chart] executing {len(code)} chars of chart code")
        ns = {'alt': alt, 'pd': pd, 'df': df, 'data': df.to_dict(orient='records')}
        exec(code, ns)
        chart = ns.get('chart')
        if chart is None:
            print("[Chart] exec ran OK but 'chart' variable not found in namespace")
            # Fallback check for other common variable names
            for var_name in ['c', 'vis', 'plot']:
                if ns.get(var_name) is not None:
                    chart = ns.get(var_name)
                    print(f"[Chart] Fallback: found chart in variable '{var_name}'")
                    break
            
        if chart is not None:
            chart.to_dict()   # validate schema — catches Altair SchemaValidationError
            print("[Chart] chart built and validated successfully")
            # --- DEBUG: Verify if 'mark': 'bar' exists in the generated spec ---
            spec_json = chart.to_json()
            print(f"[Chart DEBUG] Spec contains 'bar' mark: {'\"mark\": \"bar\"' in spec_json or '\"type\": \"bar\"' in spec_json}")
            if len(spec_json) < 2000:
                print(f"[Chart DEBUG] Full Spec: {spec_json}")
            else:
                print(f"[Chart DEBUG] Spec Preview: {spec_json[:1000]}...")
            # ------------------------------------------------------------------
            return chart
        else:
            print("[Chart] Failed to find chart variable in generated code.")
            return None
    except Exception as e:
        print(f"[Chart] ERROR: {e}")
        print(f"[Chart] Available Columns: {list(df.columns)}")
        print(f"[Chart] Final code attempted:\n{code}")
        return None


def get_page(df: pd.DataFrame, page: int) -> tuple[pd.DataFrame, str]:
    if df is None or df.empty:
        return pd.DataFrame(), "No data"
    total = len(df)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    s = (page - 1) * PAGE_SIZE
    e = min(s + PAGE_SIZE, total)
    info = f"Page {page} of {total_pages}  ·  Rows {s+1:,}–{e:,} of {total:,}"
    return df.iloc[s:e].reset_index(drop=True), info


# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def fmt_explanation(text: str) -> str:
    if not text:
        return '<p class="ab-insight-empty">Run an analysis above to see AI-generated insights.</p>'
    return f'<p class="ab-insight-text">{text}</p>'


def fmt_page_info(info: str) -> str:
    if not info or info == "No data":
        return ''
    return f'<span class="ab-pg-info">{info}</span>'


def fmt_kpi(df: pd.DataFrame, elapsed: float, cached: bool = False) -> str:
    if df is None or df.empty:
        return ''
    rows = len(df)
    cols = len(df.columns)
    t = "Cached" if cached else (f"{elapsed:.1f}s" if elapsed < 60 else f"{elapsed/60:.1f}m")
    return f'''<div class="ab-kpi-row">
  <div class="ab-kpi-card">
    <span class="ab-kv">{rows:,}</span>
    <span class="ab-kl">Rows</span>
  </div>
  <div class="ab-kpi-card">
    <span class="ab-kv">{cols}</span>
    <span class="ab-kl">Columns</span>
  </div>
  <div class="ab-kpi-card">
    <span class="ab-kv">{t}</span>
    <span class="ab-kl">Query Time</span>
  </div>
  <div class="ab-kpi-card ab-kpi-src">
    <span class="ab-kv">SQL&nbsp;Server</span>
    <span class="ab-kl">Data Source</span>
  </div>
</div>'''


# ─────────────────────────────────────────────────────────────────────────────
# Main handler
# ─────────────────────────────────────────────────────────────────────────────

def process_request(message: str):
    """Returns (sql, df_page, page_info, chart, explanation, full_df, page_num, elapsed, cached)."""
    try:
        if not message.strip():
            return ("", pd.DataFrame(), "", None, "", pd.DataFrame(), 1, 0.0, False)

        cached = _query_cache.get(message)
        if cached is not None:
            print(f"[Cache HIT] {message[:60]}")
            sql, full_df_json, cs, expl = cached
            full_df = pd.DataFrame(json.loads(full_df_json)) if full_df_json != "[]" else pd.DataFrame()
            df_page, pg = get_page(full_df, 1)
            return (sql, df_page, pg, build_chart(cs, full_df), expl, full_df, 1, 0.0, True)

        t0 = time.time()
        results = asyncio.run(run_pipeline_with_retry(message))
        elapsed = time.time() - t0

        sql = clean_sql(results.get('sql_query', ''))
        qr = parse_query_results(results.get('query_results', '{}'))

        if not qr.get('success', False):
            err = qr.get('error', 'Unknown error')
            return (f"-- Error\n{sql}\n-- {err}", pd.DataFrame(), "", None,
                    f"Query failed: {err}", pd.DataFrame(), 1, elapsed, False)

        data_list = qr.get('data', [])
        if not data_list:
            return (sql, pd.DataFrame(), "No rows returned.", None,
                    "The query executed successfully but returned no data.",
                    pd.DataFrame(), 1, elapsed, False)

        full_df = pd.DataFrame(data_list)
        df_page, pg = get_page(full_df, 1)
        cs   = results.get('chart_spec', '')
        expl = results.get('explanation_text', '')

        _query_cache.set(message, (sql, full_df.to_json(orient='records'), cs, expl))
        return (sql, df_page, pg, build_chart(cs, full_df), expl, full_df, 1, elapsed, False)

    except Exception as e:
        import traceback; traceback.print_exc()
        err = f"Error: {e}"
        return (err, pd.DataFrame(), "", None, err, pd.DataFrame(), 1, 0.0, False)


def navigate_page(direction: int, cur: int, df: pd.DataFrame):
    try:
        if df is None or df.empty:
            return pd.DataFrame(), "", cur
        new_p = cur + direction
        df_p, pg = get_page(df, new_p)
        total = max(1, (len(df) + PAGE_SIZE - 1) // PAGE_SIZE)
        return df_p, pg, max(1, min(new_p, total))
    except Exception:
        return pd.DataFrame(), "Navigation error", cur


# ─────────────────────────────────────────────────────────────────────────────
# Example questions
# ─────────────────────────────────────────────────────────────────────────────

_Q = [
    "What are the top 10 products by revenue?",
    "Show monthly sales trend for 2024",
    "Which country had the highest sales in 2023?",
    "Compare Internet Sales vs Reseller revenue",
    "Top 5 sales reps by quota attainment?",
    "Revenue breakdown by product category",
]

# ─────────────────────────────────────────────────────────────────────────────
# CSS — loaded from style.css at startup
# (Gradio 6 css_paths in launch() is the correct API; css= in Blocks is legacy)
# ─────────────────────────────────────────────────────────────────────────────

_CSS = Path(__file__).with_name("style.css").read_text(encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────────
# Gradio UI
# ─────────────────────────────────────────────────────────────────────────────

with gr.Blocks(title="AdventureBikes BI Agent") as demo:

    full_df_state = gr.State(pd.DataFrame())
    page_state    = gr.State(1)

    # ── Header ───────────────────────────────────────────────────────────────
    gr.HTML("""
<style>
  /* NUCLEAR CSS OVERRIDE */
  .ab-actions {
    display: flex !important;
    justify-content: center !important;
    gap: 20px !important;
    padding: 20px !important;
  }
  button.ab-primary {
    background-color: #008000 !important;
    background: #008000 !important;
    color: white !important;
    width: 120px !important;
    height: 40px !important;
    min-width: 120px !important;
    min-height: 40px !important;
    border-radius: 50px !important;
    font-weight: bold !important;
    border: none !important;
    box-shadow: 0 0 15px rgba(46, 204, 113, 0.6) !important;
  }
  button.ab-ghost {
    background-color: #FF0000 !important;
    background: #FF0000 !important;
    color: white !important;
    width: 120px !important;
    height: 40px !important;
    min-width: 120px !important;
    min-height: 40px !important;
    border-radius: 50px !important;
    font-weight: bold !important;
    border: none !important;
    box-shadow: 0 0 15px rgba(255, 0, 0, 0.6) !important;
  }
</style>
<header class="ab-nav">
  <div class="ab-nav-l">
    <h1 class="ab-main-title">AdventureBikes BI Agent</h1>
  </div>
  <div class="ab-nav-r">
    <span class="ab-model">Gemini 2.5 Flash</span>
    <div class="ab-live"><span class="ab-dot"></span><span>Online</span></div>
  </div>
</header>""")

    # ── Hero: chips + input ───────────────────────────────────────────────────
    with gr.Column(elem_classes=["ab-hero"]):
        gr.HTML("""
<div class="ab-htitle">
  <h1>Ask your data anything</h1>
  <p>Natural language · SQL · Visualization · Insight</p>
</div>""")

        # ── Suggestion chips ──────────────────────────────────────────────────────
        with gr.Row(elem_classes=["ab-chips"]):
            c1 = gr.Button(_Q[0], elem_classes=["ab-chip"], size="sm")
            c2 = gr.Button(_Q[1], elem_classes=["ab-chip"], size="sm")
            c3 = gr.Button(_Q[2], elem_classes=["ab-chip"], size="sm")
            c4 = gr.Button(_Q[3], elem_classes=["ab-chip"], size="sm")
            c5 = gr.Button(_Q[4], elem_classes=["ab-chip"], size="sm")
            c6 = gr.Button(_Q[5], elem_classes=["ab-chip"], size="sm")

        with gr.Group(elem_classes=["ab-search"]):
            user_input = gr.Textbox(
                placeholder="e.g.  Compare revenue vs quota by country for 2024...",
                lines=2,
                show_label=False,
                container=False,
                elem_classes=["ab-input"],
            )
            with gr.Row(elem_classes=["ab-actions"]):
                clear_btn   = gr.Button("Clear",     elem_classes=["ab-ghost"],   size="sm")
                analyze_btn = gr.Button("Analyze",   elem_classes=["ab-primary"], size="lg",
                                        variant="primary")

    # ── Results ───────────────────────────────────────────────────────────────
    with gr.Column(elem_classes=["ab-results"]):

        kpi_out = gr.HTML('')

        with gr.Tabs(elem_classes=["ab-tabs"]):

            # Overview: chart + insight
            with gr.Tab("Overview", id=0):
                chart_out = gr.Plot(
                    show_label=False,
                    elem_id="ab-chart",
                )
                with gr.Group(elem_classes=["ab-insight"]):
                    expl_out = gr.HTML(
                        '<p class="ab-insight-empty">Run an analysis above to see AI-generated insights.</p>'
                    )

            # Data Table
            with gr.Tab("Data Table", id=1):
                pg_info  = gr.HTML('')
                data_out = gr.DataFrame(
                    show_label=False,
                    wrap=False,
                    max_height=520,
                    elem_id="ab-table",
                )
                with gr.Row(elem_classes=["ab-pgrow"]):
                    prev_btn = gr.Button("← Previous", elem_classes=["ab-pgbtn"], size="sm")
                    gr.HTML("", scale=5)
                    next_btn = gr.Button("Next →",      elem_classes=["ab-pgbtn"], size="sm")

            # SQL Query
            with gr.Tab("SQL Query", id=2):
                sql_out = gr.Code(
                    value="-- Run an analysis to see the generated SQL.",
                    language="sql",
                    show_label=False,
                    lines=28,
                    elem_id="ab-sql",
                )

    # ── Event handlers ────────────────────────────────────────────────────────

    def run_analysis(msg):
        sql, dfp, pg, ch, ex, fdf, pn, el, ca = process_request(msg)
        return (
            sql,
            dfp,
            fmt_page_info(pg),
            ch,
            fmt_explanation(ex),
            fdf,
            pn,
            fmt_kpi(fdf, el, ca),
        )

    _OUT = [sql_out, data_out, pg_info, chart_out, expl_out,
            full_df_state, page_state, kpi_out]

    analyze_btn.click(fn=run_analysis, inputs=[user_input], outputs=_OUT)
    user_input.submit(fn=run_analysis, inputs=[user_input], outputs=_OUT)

    # Pagination
    def go_prev(p, df):
        dfp, pg, np_ = navigate_page(-1, p, df)
        return dfp, fmt_page_info(pg), np_

    def go_next(p, df):
        dfp, pg, np_ = navigate_page(+1, p, df)
        return dfp, fmt_page_info(pg), np_

    prev_btn.click(fn=go_prev, inputs=[page_state, full_df_state],
                   outputs=[data_out, pg_info, page_state])
    next_btn.click(fn=go_next, inputs=[page_state, full_df_state],
                   outputs=[data_out, pg_info, page_state])

    # Clear
    def clear_all():
        return (
            "",
            "-- Run an analysis to see the generated SQL.",
            pd.DataFrame(),
            '',
            None,
            '<p class="ab-insight-empty">Run an analysis above to see AI-generated insights.</p>',
            pd.DataFrame(),
            1,
            '',
        )

    clear_btn.click(
        fn=clear_all,
        inputs=None,
        outputs=[user_input, sql_out, data_out, pg_info,
                 chart_out, expl_out, full_df_state, page_state, kpi_out],
    )

    # Wire chips → fill input
    for btn, q in zip([c1, c2, c3, c4, c5, c6], _Q):
        btn.click(fn=lambda t=q: t, outputs=[user_input])


if __name__ == "__main__":
    demo.launch(
        css=_CSS
    )
