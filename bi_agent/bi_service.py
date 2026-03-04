"""
Business Intelligence Service Module

This module provides a clean interface for database operations,
keeping the app.py focused on UI and agent orchestration.

Also exports _query_cache — a module-level LRU cache for pipeline results.
"""

import pandas as pd
import json
from collections import OrderedDict
from typing import Dict, Tuple, Optional, Any


# ============================================================================
# In-memory LRU cache for pipeline results
# ============================================================================

class QueryCache:
    """
    Thread-safe LRU cache for BI pipeline results.

    Key   = question.strip().lower()
    Value = (sql_query, df, chart, explanation_text)
    Evicts the oldest entry when max_size is exceeded.
    """

    def __init__(self, max_size: int = 50):
        self._cache: OrderedDict = OrderedDict()
        self._max_size = max_size

    def get(self, question: str) -> Any:
        """Return cached result or None if not cached."""
        key = question.strip().lower()
        if key in self._cache:
            self._cache.move_to_end(key)   # Mark as recently used
            return self._cache[key]
        return None

    def set(self, question: str, value: Any) -> None:
        """Store a result, evicting the oldest entry if at capacity."""
        key = question.strip().lower()
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)   # Remove oldest

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, question: str) -> bool:
        return question.strip().lower() in self._cache


# Module-level cache instance shared across all requests
_query_cache = QueryCache(max_size=50)
from sqlalchemy.engine import Engine

from .db_config import create_db_engine, get_schema_info, validate_connection
from .sql_executor import execute_query


class BIService:
    """Service class for Business Intelligence operations."""

    def __init__(self, server: str, database: str, username: str, password: str):
        """
        Initialize BI Service with database credentials.

        Args:
            server: SQL Server hostname
            database: Database name
            username: Database username
            password: Database password
        """
        self.server = server
        self.database = database
        self.username = username
        self.password = password
        self.engine: Optional[Engine] = None
        self.schema_info: Optional[str] = None

    def connect(self) -> Tuple[bool, str]:
        """
        Connect to the database and validate connection.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            self.engine = create_db_engine(
                self.server,
                self.database,
                self.username,
                self.password
            )

            is_connected, message = validate_connection(self.engine)
            return is_connected, message

        except Exception as e:
            return False, f"Connection error: {str(e)}"

    def load_schema(self, max_tables: int = 20) -> str:
        """
        Load database schema information.

        Args:
            max_tables: Maximum number of tables to include

        Returns:
            Formatted schema string
        """
        if self.engine is None:
            raise RuntimeError("Not connected to database. Call connect() first.")

        self.schema_info = get_schema_info(self.engine, max_tables=max_tables)
        return self.schema_info

    def execute_sql(self, sql_query: str) -> Dict:
        """
        Execute a SQL query and return results.

        Args:
            sql_query: SQL query to execute

        Returns:
            Dictionary with keys: success, data (DataFrame), error, row_count, columns
        """
        if self.engine is None:
            return {
                'success': False,
                'data': None,
                'error': 'Not connected to database',
                'row_count': 0,
                'columns': []
            }

        return execute_query(self.engine, sql_query)

    def prepare_data_for_agents(self, df: pd.DataFrame, sql_query: str = "") -> str:
        """
        Prepare query results as a formatted string for agents.

        Args:
            df: Query results as DataFrame
            sql_query: Original SQL query (optional)

        Returns:
            Formatted string with data summary, sample, and statistics
        """
        if df is None or df.empty:
            return "No data available"

        data_summary = {
            'columns': df.columns.tolist(),
            'row_count': len(df),
            'sample_data': df.head(10).to_dict(orient='records'),
            'dtypes': {col: str(dtype) for col, dtype in df.dtypes.items()}
        }

        # Build formatted prompt
        prompt = f"""Here are the query results:
"""

        if sql_query:
            prompt += f"\nSQL Query: {sql_query}\n"

        prompt += f"""
Results: {len(df)} rows returned

Columns: {', '.join(data_summary['columns'])}
Data Types: {json.dumps(data_summary['dtypes'])}

Sample Data (first 10 rows):
{json.dumps(data_summary['sample_data'], indent=2)}
"""

        # Add summary statistics if there are numeric columns
        numeric_cols = df.select_dtypes(include=['number']).columns.tolist()
        if numeric_cols:
            prompt += f"""
Summary Statistics:
{df.describe().to_string()}
"""

        return prompt

    def get_schema_for_sql_generation(self, question: str) -> str:
        """
        Get formatted prompt for SQL generation agent.

        Args:
            question: User's natural language question

        Returns:
            Formatted prompt with schema and question
        """
        if self.schema_info is None:
            raise RuntimeError("Schema not loaded. Call load_schema() first.")

        return f"""{self.schema_info}

User Question: {question}
"""

    def close(self):
        """Close database connection."""
        if self.engine:
            self.engine.dispose()
            self.engine = None
