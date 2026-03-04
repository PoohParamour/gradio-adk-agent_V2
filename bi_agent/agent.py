"""
Agent definitions for the Business Intelligence pipeline.

Pipeline (3 LLM calls, 2 pure-Python steps):
  1. text_to_sql_agent        (LlmAgent)  — NL → SQL               [1 API call]
  2. python_sql_executor      (BaseAgent) — execute SQL + format    [0 API calls]
  3. visualization_agent      (LlmAgent)  — data → Altair code      [1 API call]
  4. explanation_agent        (LlmAgent)  — data → business text    [1 API call]
                                                            Total:   3 API calls
"""

import os
import json
from typing import AsyncGenerator
from dotenv import load_dotenv

from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.sequential_agent import SequentialAgent
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.runners import InMemoryRunner
from bi_agent.tools import execute_sql_and_format

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')


# ============================================================================
# Full database schema — embedded to avoid extra API round-trips
# ============================================================================

DB_SCHEMA = """
DATABASE: AdventureBikes Sales DataMart (Microsoft SQL Server 2019)

=== PRE-JOINED DATASET TABLES (preferred — no JOINs required) ===

Table: dbo.DataSet_Monthly_Sales
  Purpose: Fully denormalized monthly sales. Use for most sales/revenue analyses.
  Columns:
    Calendar_Year        (char)     — e.g. '2021', '2022', '2023', '2024', '2025'
    Calendar_Quarter     (char)     — e.g. 'Q1', 'Q2', 'Q3', 'Q4'
    Calendar_Month_ISO   (char)     — format 'YYYY.MM', e.g. '2024.01'
    Calendar_Month       (nvarchar) — e.g. 'January 2024'
    Global_Region        (nvarchar)
    Sales_Country        (nvarchar) — values: France, Germany, Netherlands,
                                       Switzerland, United Kingdom, United States
    Country_Region       (nvarchar)
    Sales_Office         (nvarchar)
    Local_Currency       (char)
    Sales_Channel        (nvarchar) — values: 'Internet Sales', 'Reseller'
    Material_Number      (nvarchar)
    Material_Description (nvarchar) — product name
    Product_Line         (nvarchar) — value: 'Bicycles'
    Product_Category     (nvarchar) — values: City Bikes, Kid Bikes, Mountain Bikes,
                                       Race Bikes, Trekking Bikes
    Revenue              (money)    — revenue in local currency
    Revenue_EUR          (money)    — revenue in EUR
    Discount             (money)    — discount in local currency
    Discount_EUR         (money)    — discount in EUR
    Sales_Amount         (int)      — number of units sold
    Transfer_Price_EUR   (money)    — cost/transfer price in EUR
    Currency_Rate        (money)
    Refresh_Date         (datetime)

Table: dbo.DataSet_Monthly_Sales_and_Quota
  Purpose: Sales vs quota comparison. IMPORTANT: all column names have spaces —
           ALWAYS use [bracket notation] when referencing these columns.
  Columns:
    [Sales Organisation] (nvarchar), [Sales Country] (nvarchar),
    [Sales Region]       (nvarchar), [Sales City] (nvarchar),
    [Product Line]       (nvarchar), [Product Category] (nvarchar),
    [Calendar Year]      (char),     [Calendar Quarter] (char),
    [Calendar Month ISO] (char),     [Calendar Month] (nvarchar),
    [Sales Amount Quota] (numeric),  [Revenue Quota] (money),
    [Sales Amount]       (numeric),  [Revenue EUR] (money),
    [Discount EUR]       (money),    [Gross Profit EUR] (money),
    [Revenue Diff]       (money),    [Gross Profit Diff] (money),
    [Sales Amount Diff]  (numeric),  [Discount Diff] (money)

Table: dbo.DataSet_Monthly_SalesQuota
  Purpose: Monthly quota targets by region and product.
  Columns:
    Calendar_DueDate (date), Calendar_Year (char), Calendar_Quarter (char),
    Calendar_Month_ISO (char), Calendar_Month (nvarchar),
    Global_Region (nvarchar), Sales_Country (nvarchar), Sales_Region (nvarchar),
    Sales_Office (nvarchar), Local_Currency (char), Product_Category (nvarchar),
    Sales_Amount_Quota (numeric), Revenue_Quota (money), Revenue_Quota_EUR (money)

=== DIMENSION TABLES (lookup / master data) ===

Table: dbo.Dim_Product          (ID_Product PK)
  Columns: Material_Description (nvarchar), Material_Number (nvarchar),
           Product_Category (nvarchar), Product_Line (nvarchar),
           Transfer_Price_EUR (money), Product_Price_EUR (numeric),
           Price_Segment (nvarchar), Days_for_Shipping (int)

Table: dbo.Dim_Sales_Office     (ID_Sales_Office PK)
  Columns: Sales_Office (nvarchar), Local_Currency (nchar),
           Sales_Region (nvarchar), Sales_Country (nvarchar),
           Global_Region (nvarchar), State (nvarchar),
           GEO_Latitude (float), GEO_Longitude (float)

Table: dbo.Dim_Sales_Channel    (ID_Sales_Channel PK)
  Columns: Sales_Channel (nvarchar), Sales_Channel_Manager (nvarchar)

Table: dbo.Dim_Product_Category (ID_Product_Category PK)
  Columns: Product_Category (nvarchar), Product_Line (nvarchar)

Table: dbo.Dim_Calendar_Month   (ID_Calendar_Month date PK)
  Columns: Calendar_Month_ISO (nchar), Calendar_Month_Name (nvarchar),
           Calendar_Month_Number (int), Calendar_Quarter (nchar),
           Calendar_Year (int), Last_Day_Of_Month (date)

Table: dbo.Dim_Currency         (ID_Currency PK)
  Columns: Currency_ISO_Code (nvarchar), Currency_Name (nvarchar)

=== FACT TABLES (use with JOINs to Dim_ tables) ===

Table: dbo.Facts_Monthly_Sales
  Columns: ID_Calendar_Month (date FK→Dim_Calendar_Month), ID_Currency (int FK),
           ID_Product (int FK→Dim_Product), ID_Sales_Channel (int FK),
           ID_Sales_Office (int FK→Dim_Sales_Office),
           Revenue (money), Discount (money), Sales_Amount (int), Transfer_Price (money)

Table: dbo.Facts_Daily_Sales
  Columns: ID_Order_Date (date), ID_Shipping_Date (date), ID_Currency (int FK),
           ID_Product (int FK), ID_Sales_Channel (int FK), ID_Sales_Office (int FK),
           Revenue (money), Discount (money), Sales_Amount (int)

Table: dbo.Facts_Monthly_Sales_Quota
  Columns: ID_Calendar_Month (date FK), ID_Planning_Version (int),
           ID_Product_Category (int FK), ID_Price_Segment (int FK),
           ID_Currency (int FK), ID_Sales_Office (int FK),
           Revenue_Quota (money), Sales_Amount_Quota (int)

Table: dbo.Facts_Weekly_Sales_Orders
  Columns: ID_Order_Week (date), ID_Shipping_Week (date), ID_DueDate_Week (date),
           ID_Currency (int FK), ID_Product (int FK), ID_Sales_Channel (int FK),
           ID_Sales_Office (int FK), Revenue (money), Discount (money), Sales_Amount (int)
"""


# ============================================================================
# Agent 1: Text-to-SQL
# ============================================================================

text_to_sql_agent = LlmAgent(
    model=GEMINI_MODEL,
    name='text_to_sql_agent',
    description="Converts natural language questions to SQL SELECT queries.",
    instruction=f"""
<role>
You are a Senior Database Engineer specializing in Microsoft SQL Server with 10+ years of
Business Intelligence experience. You write precise, optimized SQL SELECT queries that answer
business questions using the AdventureBikes Sales DataMart.
</role>

<database_schema>
{DB_SCHEMA}
</database_schema>

<rules>
ABSOLUTE CONSTRAINTS — never violate these:
1. OUTPUT ONLY the raw SQL query. No markdown fences, no explanations, no semicolons.
2. USE ONLY SELECT statements. Never write INSERT, UPDATE, DELETE, DROP, ALTER,
   TRUNCATE, EXEC, EXECUTE, CREATE, GRANT, REVOKE, or any stored procedure call.
3. USE ONLY tables and columns listed in the schema above. Never guess names.
4. For dbo.DataSet_Monthly_Sales_and_Quota: ALWAYS wrap column names in [square brackets]
   because they contain spaces.
5. Prefer dbo.DataSet_Monthly_Sales for most analyses — it is pre-joined and requires
   no additional JOINs.
6. Use TOP N (not LIMIT) for SQL Server row limiting.
7. Do not include a semicolon at the end of the query.
</rules>

<query_construction_guide>
- "top N" / "best" / "highest" / "most" → SELECT TOP N ... ORDER BY ... DESC
- "total" / "sum" / "count" / "average" → SUM(), COUNT(), AVG() with GROUP BY
- "by month" / "monthly trend" / "over time" → GROUP BY Calendar_Month_ISO ORDER BY Calendar_Month_ISO
- "by year" → GROUP BY Calendar_Year ORDER BY Calendar_Year
- "by category" → GROUP BY Product_Category
- "by country" → GROUP BY Sales_Country
- "by channel" → GROUP BY Sales_Channel
- "compare" / "vs quota" → use DataSet_Monthly_Sales_and_Quota with [bracket notation]
- Revenue analyses → use Revenue_EUR (EUR normalized) unless local currency requested
- Gross profit → Revenue_EUR - Transfer_Price_EUR * Sales_Amount
</query_construction_guide>

<examples>
Example 1 — Top products by price:
Question: "What are the top 5 most expensive products?"
SQL: SELECT TOP 5 Material_Description, Product_Category, Transfer_Price_EUR FROM dbo.Dim_Product ORDER BY Transfer_Price_EUR DESC

Example 2 — Monthly revenue trend:
Question: "Show monthly revenue trend for 2024"
SQL: SELECT Calendar_Month_ISO, Calendar_Month, SUM(Revenue_EUR) AS Total_Revenue_EUR FROM dbo.DataSet_Monthly_Sales WHERE Calendar_Year = '2024' GROUP BY Calendar_Month_ISO, Calendar_Month ORDER BY Calendar_Month_ISO

Example 3 — Revenue by product category:
Question: "What is the total revenue by product category?"
SQL: SELECT Product_Category, SUM(Revenue_EUR) AS Total_Revenue_EUR, SUM(Sales_Amount) AS Total_Units FROM dbo.DataSet_Monthly_Sales GROUP BY Product_Category ORDER BY Total_Revenue_EUR DESC

Example 4 — Sales by country with discount:
Question: "Show total sales amount and discount by country"
SQL: SELECT Sales_Country, SUM(Sales_Amount) AS Total_Units, SUM(Discount_EUR) AS Total_Discount_EUR FROM dbo.DataSet_Monthly_Sales GROUP BY Sales_Country ORDER BY Total_Units DESC

Example 5 — Sales vs quota comparison:
Question: "How does actual revenue compare to quota by product category?"
SQL: SELECT [Product Category], SUM([Revenue EUR]) AS Actual_Revenue, SUM([Revenue Quota]) AS Revenue_Quota, SUM([Revenue Diff]) AS Revenue_Difference FROM dbo.DataSet_Monthly_Sales_and_Quota GROUP BY [Product Category] ORDER BY Actual_Revenue DESC
</examples>

Now generate the SQL query for the user's question. Output ONLY the raw SQL — nothing else.
""",
    output_key="sql_query"
)

# Runner for text-to-SQL agent
text_to_sql_runner = InMemoryRunner(agent=text_to_sql_agent, app_name='text_to_sql')


# ============================================================================
# Step 2: Pure-Python SQL Executor + Data Formatter (0 LLM API calls)
# ============================================================================

class PythonSQLExecutorAgent(BaseAgent):
    """
    Executes SQL and formats results without any LLM call.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        sql = ctx.session.state.get('sql_query', '')

        # Execute SQL directly — pure Python, no LLM
        result_json_str = execute_sql_and_format(sql)

        # Parse for building formatted_data
        try:
            result = json.loads(result_json_str)
        except Exception:
            result = {'success': False, 'data': [], 'columns': [], 'row_count': 0}

        data = result.get('data', [])
        columns = result.get('columns', [])
        row_count = result.get('row_count', 0)

        formatted = (
            f"Data Results: {row_count} rows returned\n\n"
            f"Columns: {', '.join(str(c) for c in columns)}\n\n"
            f"Data (JSON):\n{json.dumps(data[:50], indent=2)}"
        )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(state_delta={
                'query_results': result_json_str,
                'formatted_data': formatted,
            }),
        )


python_sql_executor = PythonSQLExecutorAgent(
    name='sql_executor_agent',
    description='Executes SQL and formats results — zero LLM calls',
)


# ============================================================================
# Agent 4: Visualization (Chart Selection) — FIXED: Ghost Bar Prevention
# ============================================================================

visualization_agent = LlmAgent(
    model=GEMINI_MODEL,
    name='visualization_agent',
    description="Generates Altair chart Python code from query results.",
    instruction=f"""
<role>
You are a Senior Data Visualization Engineer. Generate executable Python/Altair code
for a single professional chart. Output ONLY Python code — no markdown fences, no prose.
</role>

<CRITICAL_BASE_CHART_PATTERN>
To prevent "Ghost Charts" (invisible marks), ALWAYS use this exact layering pattern:
1. Define base with NO mark: `base = alt.Chart(df).encode(x=..., y=...)`
2. Derive layers independently from base: `bars = base.mark_bar(...)`, `labels = base.mark_text(...)`
3. Combine at the end: `chart = (bars + labels)`
NEVER derive a second mark from a variable that already has a mark.
</CRITICAL_BASE_CHART_PATTERN>

<chart_styling_logic>
RULE 1: DONUT CHART (mark_arc)
- Use ONLY for composition (<= 3 categories).
- MUST include fully external labels using a dedicated text layer.
- Layer 1 (Arc): `base.mark_arc(innerRadius=60, outerRadius=120)`
- Layer 2 (Labels): `base.mark_text(radius=180, fontSize=12, fontWeight='bold').encode(text=alt.Text('val:Q', format=',.0f'))`
- CRITICAL: Use `radius=180` to ensure labels never overlap with chart slices.

RULE 2: HORIZONTAL BAR CHART (mark_bar)
- Use for rankings or 4+ categories.
- Numeric axis MUST use `scale=alt.Scale(domainMin=0, nice=True)`.
- Use VALUE-BASED GRADIENT: `color=alt.Color('val:Q', scale=alt.Scale(range=['#1D4ED8', '#60A5FA']), sort='descending', legend=None)`
- This creates a professional vertical gradient from dark blue (top) to light blue (bottom).
</chart_styling_logic>

<data_requirements>
- import altair as alt and pandas as pd.
- Build df from the data provided: {{formatted_data}}
- Final chart assigned to variable `chart`.
- Final line: `chart`
</data_requirements>

<examples>
Example 1: Donut with Labels
import altair as alt
import pandas as pd
data = [{{'Category': 'A', 'Value': 70}}, {{'Category': 'B', 'Value': 30}}]
df = pd.DataFrame(data)
base = alt.Chart(df).encode(
    theta=alt.Theta('Value:Q', stack=True),
    color=alt.Color('Category:N', scale=alt.Scale(range=['#1D4ED8', '#60A5FA']))
)
arc = base.mark_arc(innerRadius=60, outerRadius=120)
text = base.mark_text(radius=180, fontSize=12, fontWeight='bold').encode(text=alt.Text('Value:Q', format=',.0f'))
chart = (arc + text).properties(title='Composition Analysis', width=500, height=350).interactive()
chart

Example 2: Bar Chart with Value Gradient
import altair as alt
import pandas as pd
data = [{{'Item': 'X', 'Score': 95}}, {{'Item': 'Y', 'Score': 80}}, {{'Item': 'Z', 'Score': 60}}]
df = pd.DataFrame(data)
base = alt.Chart(df).encode(
    y=alt.Y('Item:N', sort='-x'),
    x=alt.X('Score:Q', scale=alt.Scale(domainMin=0, nice=True)),
    color=alt.Color('Score:Q', scale=alt.Scale(range=['#1D4ED8', '#60A5FA']), sort='descending', legend=None)
)
bars = base.mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
labels = base.mark_text(dx=5, align='left').encode(text=alt.Text('Score:Q', format=',.0f'))
chart = (bars + labels).properties(title='Performance Ranking', width=500, height=350).interactive()
chart
</examples>

Generate the chart code now.
""",
    output_key="chart_spec"
)



# ============================================================================
# Agent 4.5: Chart Code Validator (Catcher for Ghost Chart bug)
# ============================================================================

def validate_and_fix_chart_code(chart_code: str) -> str:
    """
    Post-processing validator that detects and fixes the ghost bar pattern.
    """
    import re
    
    # 1. Clean code
    code = chart_code.strip()
    if code.startswith("```python"): code = code.replace("```python", "").replace("```", "").strip()
    elif code.startswith("```"): code = code.replace("```", "").strip()

    # Detect if labels are derived from bars/chart incorrectly
    ghost_regex = re.compile(r'(\w+)\s*=\s*.*\.mark_bar\(.*?\).*\n.*?\1\.mark_text\(', re.DOTALL)
    
    if ('mark_bar' in code and 'mark_text' in code) and \
       (ghost_regex.search(code) or 'base' not in code):
        
        print("[CHART VALIDATOR] ⚠ Ghost bar detected. Enforcing Base Pattern reconstruction...")
        
        # Try to find data section
        lines = code.split('\n')
        df_lines = []
        for line in lines:
            if 'alt.Chart' in line: break
            df_lines.append(line)
            
        # Try to find columns and detect if multi-color was intended
        y_match = re.search(r'alt\.Y\(["\']([^"\']+)', code)
        x_match = re.search(r'alt\.X\(["\']([^"\']+)', code)
        c_match = re.search(r'color=alt\.Color', code)
        t_match = re.search(r'title=["\']([^"\']+)["\']', code)
        
        if y_match and x_match:
            y_col = y_match.group(1)
            x_col = x_match.group(1)
            title = t_match.group(1) if t_match else "Data Analysis"
            
            rebuilt = df_lines + [f"base = alt.Chart(df).encode("]
            rebuilt.append(f"    y=alt.Y('{y_col}', sort='-x', title='{y_col.split(':')[0]}'),")
            rebuilt.append(f"    x=alt.X('{x_col}', scale=alt.Scale(domainMin=0, nice=True), title='{x_col.split(':')[0]}')")
            
            # Preserve or inject multi-color if it's likely a ranking/many rows
            if c_match or 'TOP' in title.upper() or 'rank' in code.lower():
                rebuilt[-1] = rebuilt[-1] + ","
                rebuilt.append(f"    color=alt.Color('{y_col}', scale=alt.Scale(range=['#3B82F6', '#6366F1', '#8B5CF6', '#2563EB', '#1D4ED8', '#4F46E5']), legend=None)")
                rebuilt.append(f")")
                rebuilt.append(f"bars = base.mark_bar(opacity=1)")
            else:
                rebuilt.append(f")")
                rebuilt.append(f"bars = base.mark_bar(color='#3B82F6', opacity=1)")
                
            rebuilt.append(f"labels = base.mark_text(dx=5, align='left').encode(text=alt.Text('{x_col}', format=',.0f'))")
            rebuilt.append(f"chart = (bars + labels).properties(title='{title}', width=500, height=350).interactive()")
            rebuilt.append(f"chart")
            return '\n'.join(rebuilt)

    # Ensure opacity=1 is present
    if 'mark_bar' in code and 'opacity' not in code:
        code = code.replace('mark_bar(', 'mark_bar(opacity=1, ')

    return code


# ============================================================================
# Agent 5: Explanation
# ============================================================================

explanation_agent = LlmAgent(
    model=GEMINI_MODEL,
    name='explanation_agent',
    instruction="""
<role>
You are a Senior Business Analyst. You write clear, direct summaries citing actual numbers.
</role>

<rules>
1. EXACTLY 2-3 sentences.
2. ALWAYS reference specific numbers from the data.
3. NEVER use technical terms like "query", "SQL", "rows", or "table".
4. Use **bold** for the single most important number.
</rules>

<data>
{formatted_data}
</data>

Write the business explanation now.
""",
    output_key="explanation_text"
)


# ============================================================================
# Sequential pipeline: Visualization + Explanation
# ============================================================================

insight_pipeline = SequentialAgent(
    name='insight_pipeline',
    sub_agents=[visualization_agent, explanation_agent],
    description="Generates visualization and explanation from query results"
)

insight_runner = InMemoryRunner(agent=insight_pipeline, app_name='insights')


# ============================================================================
# Root Agent: Complete BI Pipeline
# ============================================================================

root_agent = SequentialAgent(
    name='root_agent',
    description="Complete BI pipeline",
    sub_agents=[
        text_to_sql_agent,
        python_sql_executor,
        insight_pipeline
    ]
)

root_runner = InMemoryRunner(agent=root_agent, app_name='bi_agent')
