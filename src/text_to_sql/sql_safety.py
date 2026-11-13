from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .database import connect_readonly


class SqlValidationError(ValueError):
    pass


def apply_read_only_authorizer(connection: sqlite3.Connection) -> None:
    allowed_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
    if hasattr(sqlite3, "SQLITE_RECURSIVE"):
        allowed_actions.add(sqlite3.SQLITE_RECURSIVE)
    forbidden_functions = {"load_extension", "readfile", "writefile"}

    def authorize(action: int, arg1: str | None, arg2: str | None, *_: str) -> int:
        if action not in allowed_actions:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or arg1 or "").lower() in forbidden_functions:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)


def validate_read_only_sql(
    sql: str,
    database_path: Path,
    timeout_seconds: float = 5.0,
) -> None:
    if not sql or len(sql) > 10_000:
        raise SqlValidationError("SQL is empty or exceeds the 10,000-character limit.")

    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except SqlglotError as exc:
        raise SqlValidationError(f"SQL could not be parsed: {exc}") from exc

    if len(statements) != 1 or statements[0] is None:
        raise SqlValidationError("Provide exactly one SQL statement.")

    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise SqlValidationError("Only a single read-only SELECT query is allowed.")
    if any(isinstance(node, (exp.DML, exp.DDL)) for node in statement.walk()):
        raise SqlValidationError("Data-changing SQL is not allowed.")

    connection = connect_readonly(database_path, timeout_seconds)
    apply_read_only_authorizer(connection)
    try:
        connection.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    except sqlite3.Error as exc:
        raise SqlValidationError(f"SQLite rejected the query: {exc}") from exc
    finally:
        connection.close()
