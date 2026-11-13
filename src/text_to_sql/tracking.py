from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class TrackingAgent:
    """Persists workflow steps so the UI can report progress and errors."""

    def __init__(self, store_path: Path, run_id: str) -> None:
        self.store_path = store_path
        self.run_id = run_id

    def record(
        self,
        stage: str,
        status: str,
        detail: str,
        *,
        attempt: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        logger.info(
            "workflow run_id=%s stage=%s status=%s attempt=%s detail=%s",
            self.run_id,
            stage,
            status,
            attempt if attempt is not None else "-",
            detail,
        )
        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO workflow_events
                    (run_id, stage, status, detail, attempt, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    stage,
                    status,
                    detail[:2000],
                    attempt,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            terminal = status in {
                "awaiting_approval",
                "approved",
                "rejected",
                "expired",
                "failed",
                "email_failed",
            } or (
                stage == "execution" and status == "completed"
            )
            run_status = status if terminal else "running"
            connection.execute(
                """
                UPDATE workflow_runs
                SET status = ?, current_stage = ?, updated_at = ?,
                    attempts = COALESCE(?, attempts),
                    sql = COALESCE(?, sql),
                    approval_request_id = COALESCE(?, approval_request_id),
                    results_json = COALESCE(?, results_json),
                    error = CASE WHEN ? = 'failed' THEN ? ELSE error END
                WHERE run_id = ?
                """,
                (
                    run_status,
                    stage,
                    now,
                    attempt,
                    (metadata or {}).get("sql"),
                    (metadata or {}).get("approval_request_id"),
                    (
                        json.dumps(metadata["results"], ensure_ascii=False)
                        if "results" in (metadata or {})
                        else None
                    ),
                    status,
                    detail[:2000],
                    self.run_id,
                ),
            )


def initialize_tracking_store(store_path: Path) -> None:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                sql TEXT,
                approval_request_id TEXT,
                results_json TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS workflow_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                attempt INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS workflow_events_run_id_event_id
                ON workflow_events(run_id, event_id);
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")
        }
        if "results_json" not in columns:
            connection.execute("ALTER TABLE workflow_runs ADD COLUMN results_json TEXT")


def create_tracking_run(store_path: Path, run_id: str) -> None:
    now = time.time()
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO workflow_runs (run_id, status, current_stage, created_at, updated_at)
            VALUES (?, 'queued', 'queued', ?, ?)
            """,
            (run_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO workflow_events (run_id, stage, status, detail, created_at)
            VALUES (?, 'queued', 'completed', 'Request received; preparing workflow.', ?)
            """,
            (run_id, now),
        )


def get_tracking_run(store_path: Path, run_id: str) -> dict[str, Any] | None:
    with closing(sqlite3.connect(store_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        events = connection.execute(
            """
            SELECT event_id, stage, status, detail, attempt, metadata_json, created_at
            FROM workflow_events WHERE run_id = ? ORDER BY event_id
            """,
            (run_id,),
        ).fetchall()
    return {
        "run_id": row["run_id"],
        "status": row["status"],
        "current_stage": row["current_stage"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "attempts": row["attempts"],
        "sql": row["sql"],
        "approval_request_id": row["approval_request_id"],
        "results": json.loads(row["results_json"]) if row["results_json"] else None,
        "error": row["error"],
        "events": [
            {
                "id": event["event_id"],
                "stage": event["stage"],
                "status": event["status"],
                "detail": event["detail"],
                "attempt": event["attempt"],
                "metadata": json.loads(event["metadata_json"]),
                "created_at": event["created_at"],
            }
            for event in events
        ],
    }
