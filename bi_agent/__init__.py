"""
Business Intelligence Agent Package

Pipeline: text_to_sql_agent → python_sql_executor → insight_pipeline
API calls per query: 3 (text-to-sql + visualization + explanation)
"""

from bi_agent.agent import (
    # Root agent (main entry point for ADK web)
    root_agent,
    root_runner,
    # Individual agents
    text_to_sql_agent,
    text_to_sql_runner,
    python_sql_executor,
    visualization_agent,
    explanation_agent,
    validate_and_fix_chart_code,
    # Pipelines
    insight_pipeline,
    insight_runner,
    # Constants
    GEMINI_MODEL
)

from bi_agent.bi_service import BIService, QueryCache, _query_cache
from bi_agent.tools import DatabaseTools, execute_sql_and_format, get_database_schema

__all__ = [
    # Root agent (required for ADK web)
    'root_agent',
    'root_runner',
    # Individual agents
    'text_to_sql_agent',
    'text_to_sql_runner',
    'python_sql_executor',
    'visualization_agent',
    'explanation_agent',
    # Pipelines
    'insight_pipeline',
    'insight_runner',
    # Constants
    'GEMINI_MODEL',
    # Services, Cache, and Tools
    'BIService',
    'QueryCache',
    '_query_cache',
    'DatabaseTools',
    'execute_sql_and_format',
    'get_database_schema',
]
