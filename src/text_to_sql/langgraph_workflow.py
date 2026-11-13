from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph

from .database import get_schema
from .ollama_client import OllamaClient, OllamaError
from .sql_safety import SqlValidationError, validate_read_only_sql
from .tracking import TrackingAgent


class WorkflowState(TypedDict):
    question: str
    schema: str
    database_path: str
    max_attempts: int
    attempts: int
    sql: str
    feedback: str
    validation_error: str
    relevant: bool
    evaluation_reason: str
    exhausted: bool


class GenerationError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


def run_sql_workflow(
    *,
    question: str,
    database_path: str,
    max_attempts: int,
    query_timeout_seconds: float,
    client: OllamaClient,
    tracker: TrackingAgent,
) -> tuple[str, int, str]:
    path = database_path

    def load_schema(_: WorkflowState) -> dict[str, str]:
        started = time.monotonic()
        tracker.record("schema", "running", "Reading tables, columns, and relationships.")
        try:
            schema = get_schema(Path(path))
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            elapsed = time.monotonic() - started
            logger.error("Database schema retrieval failed elapsed_seconds=%.1f error=%s", elapsed, exc)
            tracker.record("schema", "failed", f"Could not retrieve database schema: {exc}")
            raise
        elapsed = time.monotonic() - started
        logger.info(
            "Database schema retrieved successfully elapsed_seconds=%.1f schema_characters=%s",
            elapsed,
            len(schema),
        )
        tracker.record(
            "schema",
            "completed",
            f"Database schema retrieved successfully ({len(schema)} characters).",
        )
        return {"schema": schema}

    def generate(state: WorkflowState) -> dict[str, object]:
        attempt = state["attempts"] + 1
        logger.info(
            "Starting combined SQL generation and relevance check attempt=%s", attempt
        )
        tracker.record(
            "generation",
            "running",
            "Sending one Ollama request to generate and self-check SQL.",
            attempt=attempt,
        )
        started = time.monotonic()
        try:
            generated = client.generate_and_evaluate(
                state["question"], state["schema"], state["sql"], state["feedback"]
            )
        except OllamaError as exc:
            logger.error(
                "SQL generation and relevance check failed attempt=%s "
                "elapsed_seconds=%.1f error=%s",
                attempt,
                time.monotonic() - started,
                exc,
            )
            tracker.record("generation", "failed", str(exc), attempt=attempt)
            raise
        elapsed = time.monotonic() - started
        logger.info(
            "Combined Ollama response received attempt=%s relevant=%s "
            "elapsed_seconds=%.1f sql_characters=%s",
            attempt,
            generated.relevant,
            elapsed,
            len(generated.sql),
        )
        tracker.record(
            "generation",
            "completed",
            f"Combined generation and relevance check completed ({elapsed:.1f}s).",
            attempt=attempt,
            metadata={"sql": generated.sql, "relevant": generated.relevant},
        )
        tracker.record(
            "evaluation",
            "completed" if generated.relevant else "retrying",
            generated.reason or (
                "The query is relevant."
                if generated.relevant
                else "The request is not answerable from this schema."
            ),
            attempt=attempt,
        )
        return {
            "sql": generated.sql,
            "attempts": attempt,
            "relevant": generated.relevant,
            "evaluation_reason": generated.reason,
            "feedback": "" if generated.relevant else generated.reason,
            "validation_error": "",
        }

    def validate(state: WorkflowState) -> dict[str, str]:
        attempt = state["attempts"]
        tracker.record(
            "validation",
            "running",
            "Checking single-statement, read-only SQL against SQLite.",
            attempt=attempt,
        )
        try:
            validate_read_only_sql(
                state["sql"], Path(path), query_timeout_seconds
            )
        except SqlValidationError as exc:
            message = str(exc)
            tracker.record(
                "validation",
                "retrying",
                message,
                attempt=attempt,
            )
            return {
                "validation_error": message,
                "feedback": message,
                "relevant": False,
            }
        tracker.record("validation", "completed", "SQLite accepted the read-only query plan.", attempt=attempt)
        return {"validation_error": ""}

    def retry_or_finish(state: WorkflowState) -> dict[str, bool]:
        exhausted = state["attempts"] >= state["max_attempts"]
        if exhausted:
            message = (
                f"SQL did not pass validation and relevance self-check after "
                f"{state['attempts']} attempt(s). Last feedback: {state['feedback']}"
            )
            tracker.record("workflow", "failed", message, attempt=state["attempts"])
        return {"exhausted": exhausted}

    def finish(state: WorkflowState) -> dict[str, bool]:
        tracker.record(
            "workflow",
            "running",
            "SQL passed validation and relevance evaluation.",
            attempt=state["attempts"],
            metadata={"sql": state["sql"]},
        )
        return {}

    def after_generation(state: WorkflowState) -> str:
        return "validate" if state["relevant"] else "retry"

    def after_validation(state: WorkflowState) -> str:
        return "retry" if state["validation_error"] else "finish"

    def after_retry(state: WorkflowState) -> str:
        return "end" if state["exhausted"] else "generate"

    graph = StateGraph(WorkflowState)
    graph.add_node("load_schema", load_schema)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_node("retry", retry_or_finish)
    graph.add_node("finish", finish)
    graph.set_entry_point("load_schema")
    graph.add_edge("load_schema", "generate")
    graph.add_conditional_edges(
        "generate", after_generation, {"validate": "validate", "retry": "retry"}
    )
    graph.add_conditional_edges("validate", after_validation, {"retry": "retry", "finish": "finish"})
    graph.add_conditional_edges("retry", after_retry, {"generate": "generate", "end": END})
    graph.add_edge("finish", END)

    initial_state: WorkflowState = {
        "question": question,
        "schema": "",
        "database_path": path,
        "max_attempts": max_attempts,
        "attempts": 0,
        "sql": "",
        "feedback": "",
        "validation_error": "",
        "relevant": False,
        "evaluation_reason": "",
        "exhausted": False,
    }
    try:
        state = graph.compile().invoke(initial_state)
    except OllamaError as exc:
        logger.error("LangGraph workflow could not complete its Ollama request: %s", exc)
        raise
    if not state["relevant"] or state["exhausted"]:
        raise GenerationError(
            f"SQL did not pass validation and relevance self-check after "
            f"{state['attempts']} attempt(s)."
        )
    return state["sql"], state["attempts"], state["schema"]
