from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .approvals import (
    ApprovalError,
    ApprovalInboxError,
    EmailDeliveryError,
    close_approval_inbox,
    create_approval_request,
    decide_request,
    execute_read_only,
    expire_approval_request,
    find_approval_reply,
    get_approval_request,
    get_approval_status,
    initialize_approval_store,
    mark_email_failed,
    open_approval_inbox,
    send_approval_email,
)
from .database import get_schema
from .langgraph_workflow import GenerationError, run_sql_workflow
from .ollama_client import OllamaClient, OllamaError
from .tracking import (
    TrackingAgent,
    create_tracking_run,
    get_tracking_run,
    initialize_tracking_store,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("text_to_sql").setLevel(logging.INFO)


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _float_setting(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}.")
    return value


def _boolean_setting(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{name} must be 'true' or 'false'.")
    return normalized == "true"


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)


def create_app(test_config: dict[str, Any] | None = None) -> FastAPI:
    load_dotenv(PROJECT_ROOT / ".env")

    def configured_path(name: str, default: str) -> Path:
        path = Path(os.getenv(name, default)).expanduser()
        return (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()

    settings: dict[str, Any] = {
        "DATABASE_PATH": configured_path("DATABASE_PATH", "data/college.db"),
        "APPROVAL_DB_PATH": configured_path("APPROVAL_DB_PATH", "data/approvals.db"),
        "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "OLLAMA_MODEL": os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
        "OLLAMA_TIMEOUT_SECONDS": _float_setting("OLLAMA_TIMEOUT_SECONDS", 120, 1, 120),
        "MAX_GENERATION_ATTEMPTS": _integer_setting("MAX_GENERATION_ATTEMPTS", 3, 1, 3),
        "QUERY_TIMEOUT_SECONDS": float(os.getenv("QUERY_TIMEOUT_SECONDS", "5")),
        "MAX_RESULT_ROWS": _integer_setting("MAX_RESULT_ROWS", 200, 1, 5000),
        "APPROVAL_EXPIRY_MINUTES": _integer_setting("APPROVAL_EXPIRY_MINUTES", 20, 1, 20),
        "APPROVAL_REQUIRED": _boolean_setting("APPROVAL_REQUIRED", False),
        "SMTP_HOST": os.getenv("SMTP_HOST", ""),
        "SMTP_PORT": int(os.getenv("SMTP_PORT", "587")),
        "SMTP_USERNAME": os.getenv("SMTP_USERNAME", ""),
        "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD", ""),
        "SMTP_USE_TLS": os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true",
        "MAIL_FROM": os.getenv("MAIL_FROM", ""),
        "APPROVER_EMAIL": os.getenv("APPROVER_EMAIL", "saitejaatkapuram7@gmail.com"),
        "IMAP_HOST": os.getenv("IMAP_HOST", "imap.gmail.com"),
        "IMAP_PORT": int(os.getenv("IMAP_PORT", "993")),
        "IMAP_USERNAME": os.getenv("IMAP_USERNAME", os.getenv("SMTP_USERNAME", "")),
        "IMAP_PASSWORD": os.getenv("IMAP_PASSWORD", os.getenv("SMTP_PASSWORD", "")),
        "IMAP_POLL_INTERVAL_SECONDS": _integer_setting(
            "IMAP_POLL_INTERVAL_SECONDS", 15, 5, 60
        ),
        "APPROVAL_BASE_URL": os.getenv("APPROVAL_BASE_URL", "http://localhost:5173/approval"),
        "CORS_ORIGINS": [
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if origin.strip()
        ],
    }
    if test_config:
        settings.update(test_config)
    initialize_approval_store(Path(settings["APPROVAL_DB_PATH"]))
    initialize_tracking_store(Path(settings["APPROVAL_DB_PATH"]))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.run_tasks = set()
        application.state.workflow_active = False
        application.state.stop_event = threading.Event()
        yield
        application.state.stop_event.set()
        tasks = application.state.run_tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    application = FastAPI(
        title="College Text-to-SQL API",
        version="2.0.0",
        description=(
            "LangGraph and Ollama text-to-SQL with read-only SQLite and configurable "
            "human approval."
        ),
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings["CORS_ORIGINS"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    dist = PROJECT_ROOT / "frontend" / "dist"
    assets = dist / "assets"
    if assets.is_dir():
        application.mount("/assets", StaticFiles(directory=assets), name="assets")

    def run_pipeline(
        run_id: str,
        question: str,
        stop_event: threading.Event,
    ) -> None:
        tracker = TrackingAgent(Path(settings["APPROVAL_DB_PATH"]), run_id)
        logger.info("Starting query workflow run_id=%s", run_id)
        try:
            sql, attempts, _ = run_sql_workflow(
                question=question,
                database_path=str(settings["DATABASE_PATH"]),
                max_attempts=settings["MAX_GENERATION_ATTEMPTS"],
                query_timeout_seconds=settings["QUERY_TIMEOUT_SECONDS"],
                client=OllamaClient(
                    settings["OLLAMA_BASE_URL"],
                    settings["OLLAMA_MODEL"],
                    settings["OLLAMA_TIMEOUT_SECONDS"],
                ),
                tracker=tracker,
            )
        except (GenerationError, OllamaError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            logger.error("Query workflow failed run_id=%s error=%s", run_id, exc)
            tracker.record("workflow", "failed", f"Workflow stopped: {exc}")
            return

        if not settings["APPROVAL_REQUIRED"]:
            logger.info("Approval disabled; executing read-only query run_id=%s", run_id)
            tracker.record(
                "execution",
                "running",
                "Human approval is disabled. Executing the validated query read-only.",
                attempt=attempts,
                metadata={"sql": sql},
            )
            try:
                results = execute_read_only(
                    Path(settings["DATABASE_PATH"]),
                    sql,
                    settings["QUERY_TIMEOUT_SECONDS"],
                    settings["MAX_RESULT_ROWS"],
                )
            except (sqlite3.Error, ValueError) as exc:
                tracker.record(
                    "execution",
                    "failed",
                    f"Validated query could not be executed: {exc}",
                    attempt=attempts,
                )
                return
            tracker.record(
                "execution",
                "completed",
                "Query executed read-only without email approval, as configured.",
                attempt=attempts,
                metadata={"sql": sql, "results": results},
            )
            logger.info("Read-only query completed run_id=%s", run_id)
            return

        logger.info("Approval required; preparing email and inbox watcher run_id=%s", run_id)
        created_at = time.time()
        expiry_seconds = settings["APPROVAL_EXPIRY_MINUTES"] * 60
        expires_at = created_at + expiry_seconds
        request_id, token = create_approval_request(
            Path(settings["APPROVAL_DB_PATH"]),
            question,
            sql,
            attempts,
            created_at,
            expiry_seconds,
        )
        mailbox: imaplib.IMAP4_SSL | None = None
        try:
            mailbox = open_approval_inbox(
                settings["IMAP_HOST"],
                settings["IMAP_PORT"],
                settings["IMAP_USERNAME"],
                settings["IMAP_PASSWORD"],
            )
        except ApprovalInboxError as exc:
            mark_email_failed(Path(settings["APPROVAL_DB_PATH"]), request_id, str(exc))
            tracker.record("approval_email", "failed", str(exc), attempt=attempts)
            logger.error("Approval inbox is unavailable; email was not sent run_id=%s error=%s", run_id, exc)
            return

        tracker.record(
            "approval_email",
            "running",
            f"Sending the approval email to {settings['APPROVER_EMAIL']}.",
            attempt=attempts,
            metadata={"sql": sql, "approval_request_id": request_id},
        )
        email_started = time.monotonic()
        logger.info(
            "Sending approval email run_id=%s recipient=%s",
            run_id,
            settings["APPROVER_EMAIL"],
        )
        try:
            send_approval_email(
                host=settings["SMTP_HOST"],
                port=settings["SMTP_PORT"],
                username=settings["SMTP_USERNAME"],
                password=settings["SMTP_PASSWORD"],
                use_tls=settings["SMTP_USE_TLS"],
                sender=settings["MAIL_FROM"],
                recipient=settings["APPROVER_EMAIL"],
                base_url=settings["APPROVAL_BASE_URL"],
                request_id=request_id,
                token=token,
                question=question,
                sql=sql,
                expires_at=expires_at,
            )
        except EmailDeliveryError as exc:
            logger.error("Approval email could not be sent run_id=%s error=%s", run_id, exc)
            mark_email_failed(Path(settings["APPROVAL_DB_PATH"]), request_id, str(exc))
            tracker.record("approval_email", "failed", str(exc), attempt=attempts)
            close_approval_inbox(mailbox)
            return
        logger.info(
            "Approval email sent run_id=%s elapsed_seconds=%.1f",
            run_id,
            time.monotonic() - email_started,
        )
        tracker.record(
            "approval_email",
            "completed",
            "Approval email sent with the question and generated SQL.",
            attempt=attempts,
            metadata={"sql": sql, "approval_request_id": request_id},
        )
        tracker.record(
            "approval_poll",
            "running",
            f"Waiting for APPROVE or REJECT reply from the configured approver. "
            f"Request expires in {settings['APPROVAL_EXPIRY_MINUTES']} minutes.",
            attempt=attempts,
            metadata={"sql": sql, "approval_request_id": request_id},
        )
        logger.info(
            "Monitoring approval inbox run_id=%s request_id=%s interval_seconds=%s",
            run_id,
            request_id,
            settings["IMAP_POLL_INTERVAL_SECONDS"],
        )
        last_poll_log = time.monotonic()
        processed_mail_uids: set[bytes] = set()
        try:
            while time.time() < expires_at and not stop_event.is_set():
                request_status = get_approval_status(
                    Path(settings["APPROVAL_DB_PATH"]), request_id
                )
                if request_status in {"approved", "rejected", "expired", "execution_failed"}:
                    logger.info(
                        "Approval already handled through another channel run_id=%s status=%s",
                        run_id,
                        request_status,
                    )
                    if request_status == "expired":
                        tracker.record(
                            "approval",
                            "expired",
                            "Approval expired. The query was not executed.",
                            attempt=attempts,
                            metadata={"sql": sql},
                        )
                    elif request_status == "execution_failed":
                        tracker.record(
                            "execution",
                            "failed",
                            "The approved query could not be executed.",
                            attempt=attempts,
                            metadata={"sql": sql},
                        )
                    return
                if mailbox is None:
                    try:
                        mailbox = open_approval_inbox(
                            settings["IMAP_HOST"],
                            settings["IMAP_PORT"],
                            settings["IMAP_USERNAME"],
                            settings["IMAP_PASSWORD"],
                        )
                    except ApprovalInboxError as exc:
                        if time.monotonic() - last_poll_log >= 60:
                            logger.warning(
                                "Approval inbox reconnect failed run_id=%s error=%s",
                                run_id,
                                exc,
                            )
                            tracker.record(
                                "approval_poll",
                                "running",
                                f"Inbox temporarily unavailable; retrying. {exc}",
                                attempt=attempts,
                            )
                            last_poll_log = time.monotonic()
                        stop_event.wait(settings["IMAP_POLL_INTERVAL_SECONDS"])
                        continue
                try:
                    assert mailbox is not None
                    reply = find_approval_reply(
                        mailbox,
                        request_id,
                        settings["APPROVER_EMAIL"],
                        processed_mail_uids,
                    )
                except ApprovalInboxError as exc:
                    close_approval_inbox(mailbox)
                    mailbox = None
                    if time.monotonic() - last_poll_log >= 60:
                        logger.warning(
                            "Approval inbox check failed run_id=%s error=%s; reconnecting",
                            run_id,
                            exc,
                        )
                        tracker.record(
                            "approval_poll",
                            "running",
                            f"Inbox temporarily unavailable; retrying. {exc}",
                            attempt=attempts,
                        )
                        last_poll_log = time.monotonic()
                    stop_event.wait(settings["IMAP_POLL_INTERVAL_SECONDS"])
                    continue

                if reply is not None:
                    logger.info(
                        "Approval reply received run_id=%s decision=%s sender=%s",
                        run_id,
                        reply.decision,
                        reply.sender,
                    )
                    try:
                        decision = decide_request(
                            store_path=Path(settings["APPROVAL_DB_PATH"]),
                            database_path=Path(settings["DATABASE_PATH"]),
                            request_id=request_id,
                            token=token,
                            decision=reply.decision,
                            now=time.time(),
                            query_timeout_seconds=settings["QUERY_TIMEOUT_SECONDS"],
                            max_result_rows=settings["MAX_RESULT_ROWS"],
                            decision_comment=reply.comment,
                            decided_by=reply.sender,
                        )
                    except ApprovalError as exc:
                        if "expired" in str(exc).lower():
                            tracker.record(
                                "approval",
                                "expired",
                                "Approval expired. The query was not executed.",
                                attempt=attempts,
                                metadata={"sql": sql},
                            )
                            return
                        tracker.record(
                            "approval",
                            "failed",
                            f"Could not apply the email decision: {exc}",
                            attempt=attempts,
                        )
                        return

                    comment = reply.comment or "No comment provided."
                    if reply.decision == "approve":
                        tracker.record(
                            "execution",
                            "completed",
                            f"Approved by {reply.sender}. Executed read-only SQL. "
                            f"Comment: {comment}",
                            attempt=attempts,
                            metadata={
                                "sql": sql,
                                "results": decision["results"],
                                "decision_comment": comment,
                            },
                        )
                        tracker.record(
                            "approval",
                            "approved",
                            f"Approved by {reply.sender}. Comment: {comment}",
                            attempt=attempts,
                            metadata={
                                "sql": sql,
                                "results": decision["results"],
                                "decision_comment": comment,
                                "decided_by": reply.sender,
                            },
                        )
                        logger.info("Approved SQL executed run_id=%s", run_id)
                    else:
                        tracker.record(
                            "approval",
                            "rejected",
                            f"Rejected by {reply.sender}. Comment: {comment}. "
                            "The query was not executed.",
                            attempt=attempts,
                            metadata={
                                "sql": sql,
                                "decision_comment": comment,
                                "decided_by": reply.sender,
                            },
                        )
                        logger.info("SQL request rejected run_id=%s", run_id)
                    return

                now = time.time()
                if now >= expires_at:
                    break
                if time.monotonic() - last_poll_log >= 60:
                    logger.info(
                        "Still waiting for approval reply run_id=%s seconds_remaining=%s",
                        run_id,
                        max(0, int(expires_at - now)),
                    )
                    last_poll_log = time.monotonic()
                stop_event.wait(
                    min(settings["IMAP_POLL_INTERVAL_SECONDS"], expires_at - now)
                )
        finally:
            close_approval_inbox(mailbox)

        if stop_event.is_set():
            tracker.record(
                "approval_poll",
                "failed",
                "Application stopped while waiting for the email reply. "
                "The query was not executed.",
                attempt=attempts,
            )
            logger.info("Stopped approval inbox watcher run_id=%s", run_id)
            return

        expire_approval_request(
            Path(settings["APPROVAL_DB_PATH"]), request_id, time.time()
        )
        tracker.record(
            "approval",
            "expired",
            "Approval expired. The query was not executed.",
            attempt=attempts,
            metadata={"sql": sql},
        )
        logger.info("Approval expired run_id=%s", run_id)

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        try:
            get_schema(Path(settings["DATABASE_PATH"]))
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail=f"Database is unavailable: {exc}") from exc
        return {"status": "ok"}

    @application.get("/api/schema")
    async def schema() -> dict[str, str]:
        try:
            return {"schema": get_schema(Path(settings["DATABASE_PATH"]))}
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail=f"Database is unavailable: {exc}") from exc

    @application.get("/api/config")
    async def public_config() -> dict[str, bool]:
        return {"approval_required": settings["APPROVAL_REQUIRED"]}

    @application.post("/api/query", status_code=202)
    async def submit_query(body: QueryRequest) -> dict[str, str]:
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="Question cannot be blank.")
        if application.state.workflow_active:
            raise HTTPException(
                status_code=429,
                detail="A query is already processing. Wait for it to finish before submitting another.",
            )
        run_id = uuid.uuid4().hex
        store_path = Path(settings["APPROVAL_DB_PATH"])
        create_tracking_run(store_path, run_id)
        application.state.workflow_active = True
        task = asyncio.create_task(
            asyncio.to_thread(
                run_pipeline, run_id, question, application.state.stop_event
            )
        )
        application.state.run_tasks.add(task)

        def finish_task(completed_task: asyncio.Task[Any]) -> None:
            application.state.run_tasks.discard(completed_task)
            application.state.workflow_active = False

        task.add_done_callback(finish_task)
        return {"run_id": run_id, "status": "queued"}

    @application.get("/api/runs/{run_id}")
    async def run_status(run_id: str) -> dict[str, Any]:
        result = get_tracking_run(Path(settings["APPROVAL_DB_PATH"]), run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Workflow run not found.")
        return result

    @application.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        async def event_stream():
            last_version: tuple[float, int] | None = None
            while True:
                result = await asyncio.to_thread(
                    get_tracking_run, Path(settings["APPROVAL_DB_PATH"]), run_id
                )
                if result is None:
                    yield "event: error\ndata: Workflow run not found.\n\n"
                    return
                latest_event_id = result["events"][-1]["id"] if result["events"] else 0
                version = (result["updated_at"], latest_event_id)
                if version != last_version:
                    yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"
                    last_version = version
                if result["status"] in {
                    "awaiting_approval",
                    "approved",
                    "rejected",
                    "expired",
                    "failed",
                    "email_failed",
                    "completed",
                }:
                    return
                await asyncio.sleep(5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get("/api/approval/{request_id}/{token}")
    async def approval_details(request_id: str, token: str) -> dict[str, Any]:
        try:
            result = get_approval_request(
                Path(settings["APPROVAL_DB_PATH"]), request_id, token, time.time()
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        return {
            "request_id": result["request_id"],
            "question": result["question"],
            "sql": result["sql"],
            "attempts": result["attempts"],
            "expires_at": result["expires_at"],
            "status": result["status"],
            "results": (
                json.loads(result["result_json"]) if result.get("result_json") else None
            ),
            "decision_comment": result.get("decision_comment"),
            "decided_by": result.get("decided_by"),
        }

    @application.post("/api/approval/{request_id}/{token}")
    async def approval_decision(
        request_id: str,
        token: str,
        body: ApprovalDecision,
    ) -> dict[str, Any]:
        try:
            result = decide_request(
                store_path=Path(settings["APPROVAL_DB_PATH"]),
                database_path=Path(settings["DATABASE_PATH"]),
                request_id=request_id,
                token=token,
                decision=body.decision,
                now=time.time(),
                query_timeout_seconds=settings["QUERY_TIMEOUT_SECONDS"],
                max_result_rows=settings["MAX_RESULT_ROWS"],
                decision_comment=body.comment,
                decided_by=settings["APPROVER_EMAIL"],
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        store_path = Path(settings["APPROVAL_DB_PATH"])
        with closing(sqlite3.connect(store_path)) as connection:
            row = connection.execute(
                "SELECT run_id FROM workflow_runs WHERE approval_request_id = ?",
                (request_id,),
            ).fetchone()
        if row:
            tracker = TrackingAgent(store_path, row[0])
            comment = result.get("decision_comment") or "No comment provided."
            decided_by = result.get("decided_by") or settings["APPROVER_EMAIL"]
            metadata = {
                "sql": result["sql"],
                "decision_comment": comment,
                "decided_by": decided_by,
            }
            if result["status"] == "approved":
                tracker.record(
                    "execution",
                    "completed",
                    f"Approved by {decided_by}. Executed read-only SQL. Comment: {comment}",
                    metadata={**metadata, "results": result["results"]},
                )
            tracker.record(
                "approval",
                result["status"],
                f"{'Approved' if result['status'] == 'approved' else 'Rejected'} by "
                f"{decided_by}. Comment: {comment}"
                + (
                    ""
                    if result["status"] == "approved"
                    else " The query was not executed."
                ),
                metadata={
                    **metadata,
                    **({"results": result["results"]} if result.get("results") else {}),
                },
            )
        return {
            "status": result["status"],
            "question": result["question"],
            "sql": result["sql"],
            "results": result.get("results"),
            "decision_comment": result.get("decision_comment"),
            "decided_by": result.get("decided_by"),
        }

    @application.get("/")
    async def frontend_index() -> FileResponse:
        index_file = dist / "index.html"
        if not index_file.is_file():
            raise HTTPException(
                status_code=404,
                detail="Frontend is not built. Run `cd frontend; npm install; npm run build`.",
            )
        return FileResponse(index_file)

    @application.get("/approval")
    async def frontend_approval() -> FileResponse:
        index_file = dist / "index.html"
        if not index_file.is_file():
            raise HTTPException(
                status_code=404,
                detail="Frontend is not built. Run `cd frontend; npm install; npm run build`.",
            )
        return FileResponse(index_file)

    return application


app = create_app()
