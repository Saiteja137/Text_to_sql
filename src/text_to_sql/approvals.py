from __future__ import annotations

import hashlib
import imaplib
import json
import logging
import re
import secrets
import smtplib
import sqlite3
import time
from dataclasses import dataclass
from contextlib import closing
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from email.utils import parseaddr
from urllib.parse import urlencode

from .database import connect_readonly, json_safe
from .sql_safety import (
    SqlValidationError,
    apply_read_only_authorizer,
    validate_read_only_sql,
)

logger = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


class ApprovalInboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalReply:
    decision: str
    sender: str
    comment: str


def initialize_approval_store(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_requests (
                request_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL,
                question TEXT NOT NULL,
                sql TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error TEXT
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approval_requests)")
        }
        for column in ("decision_comment", "decided_by"):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE approval_requests ADD COLUMN {column} TEXT"
                )


def create_approval_request(
    store_path: Path,
    question: str,
    sql: str,
    attempts: int,
    now: float,
    expiry_seconds: int,
) -> tuple[str, str]:
    request_id = secrets.token_urlsafe(18)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO approval_requests
                (request_id, token_hash, question, sql, attempts, created_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (request_id, token_hash, question, sql, attempts, now, now + expiry_seconds),
        )
    return request_id, token


def mark_email_failed(store_path: Path, request_id: str, error: str) -> None:
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.execute(
            "UPDATE approval_requests SET status = 'email_failed', error = ? "
            "WHERE request_id = ? AND status = 'pending'",
            (error[:1000], request_id),
        )


def expire_approval_request(store_path: Path, request_id: str, now: float) -> bool:
    with closing(sqlite3.connect(store_path)) as connection, connection:
        cursor = connection.execute(
            "UPDATE approval_requests SET status = 'expired' "
            "WHERE request_id = ? AND status = 'pending' AND expires_at <= ?",
            (request_id, now),
        )
    return cursor.rowcount == 1


def _get_request(
    store_path: Path,
    request_id: str,
    token: str,
    now: float,
) -> dict[str, Any]:
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM approval_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    if row is None:
        raise ApprovalError("Approval request not found.")
    actual_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(actual_hash, row["token_hash"]):
        raise ApprovalError("This approval link is invalid.")
    expired = row["status"] == "pending" and now > row["expires_at"]
    if expired:
        with closing(sqlite3.connect(store_path)) as connection, connection:
            connection.execute(
                "UPDATE approval_requests SET status = 'expired' WHERE request_id = ? AND status = 'pending'",
                (request_id,),
            )
    result = dict(row)
    if expired:
        result["status"] = "expired"
    return result


def get_approval_request(
    store_path: Path,
    request_id: str,
    token: str,
    now: float,
) -> dict[str, Any]:
    return _get_request(store_path, request_id, token, now)


def get_approval_status(store_path: Path, request_id: str) -> str | None:
    with closing(sqlite3.connect(store_path)) as connection:
        row = connection.execute(
            "SELECT status FROM approval_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return str(row[0]) if row else None


def decide_request(
    store_path: Path,
    database_path: Path,
    request_id: str,
    token: str,
    decision: str,
    now: float,
    query_timeout_seconds: float,
    max_result_rows: int,
    decision_comment: str = "",
    decided_by: str = "",
) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise ApprovalError("Choose approve or reject.")

    request = _get_request(store_path, request_id, token, now)
    if request["status"] != "pending":
        if request["status"] == "approved" and request["result_json"]:
            request["results"] = json.loads(request["result_json"])
            return request
        raise ApprovalError(f"This request is already {request['status']}.")
    if decision == "reject":
        with closing(sqlite3.connect(store_path)) as connection, connection:
            cursor = connection.execute(
                "UPDATE approval_requests SET status = 'rejected', decision_comment = ?, "
                "decided_by = ? WHERE request_id = ? AND status = 'pending'",
                (decision_comment[:1000], decided_by[:320], request_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalError("This request is expired or has already been handled.")
        request["status"] = "rejected"
        request["decision_comment"] = decision_comment[:1000]
        request["decided_by"] = decided_by[:320]
        return request

    with closing(sqlite3.connect(store_path)) as connection, connection:
        cursor = connection.execute(
            "UPDATE approval_requests SET status = 'executing', decision_comment = ?, "
            "decided_by = ? "
            "WHERE request_id = ? AND status = 'pending' AND expires_at >= ?",
            (decision_comment[:1000], decided_by[:320], request_id, now),
        )
        if cursor.rowcount != 1:
            raise ApprovalError("This request is expired or has already been handled.")

    try:
        validate_read_only_sql(request["sql"], database_path, query_timeout_seconds)
        results = execute_read_only(
            database_path,
            request["sql"],
            query_timeout_seconds,
            max_result_rows,
        )
    except (sqlite3.Error, SqlValidationError) as exc:
        with closing(sqlite3.connect(store_path)) as connection, connection:
            connection.execute(
                "UPDATE approval_requests SET status = 'execution_failed', error = ? "
                "WHERE request_id = ? AND status = 'executing'",
                (str(exc)[:1000], request_id),
            )
        raise ApprovalError(f"Approved query could not be executed: {exc}") from exc

    serialized = json.dumps(results, ensure_ascii=False)
    with closing(sqlite3.connect(store_path)) as connection, connection:
        connection.execute(
            "UPDATE approval_requests SET status = 'approved', result_json = ? "
            "WHERE request_id = ? AND status = 'executing'",
            (serialized, request_id),
        )
    request["status"] = "approved"
    request["results"] = results
    request["decision_comment"] = decision_comment[:1000]
    request["decided_by"] = decided_by[:320]
    return request


def execute_read_only(
    database_path: Path,
    sql: str,
    query_timeout_seconds: float,
    max_result_rows: int,
) -> dict[str, Any]:
    validate_read_only_sql(sql, database_path, query_timeout_seconds)
    connection = connect_readonly(database_path, query_timeout_seconds)
    deadline = time.monotonic() + query_timeout_seconds
    try:
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            1000,
        )
        apply_read_only_authorizer(connection)
        columns: list[str] = []
        rows: list[list[Any]] = []
        truncated = False
        cursor = connection.execute(sql)
        columns = [description[0] for description in (cursor.description or [])]
        for row in cursor.fetchmany(max_result_rows + 1):
            if len(rows) == max_result_rows:
                truncated = True
                break
            rows.append([json_safe(value) for value in row])
        return {"columns": columns, "rows": rows, "truncated": truncated}
    finally:
        connection.close()


def send_approval_email(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    sender: str,
    recipient: str,
    base_url: str,
    request_id: str,
    token: str,
    question: str,
    sql: str,
    expires_at: float,
) -> None:
    if not all((host, username, password, sender, recipient, base_url)):
        raise EmailDeliveryError(
            "Email approval is not configured. Set SMTP_HOST, SMTP_USERNAME, "
            "SMTP_PASSWORD, MAIL_FROM, APPROVER_EMAIL, and APPROVAL_BASE_URL in .env."
        )

    url = (
        f"{base_url.rstrip('/')}?{urlencode({'request_id': request_id, 'token': token})}"
    )
    from datetime import datetime, timezone

    expiry_text = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    message = EmailMessage()
    message["Subject"] = f"[Text-to-SQL approval {request_id}] Please approve or reject"
    message["From"] = sender
    message["To"] = recipient
    message["Reply-To"] = sender
    message.set_content(
        f"Question:\n{question}\n\n"
        f"Generated read-only SQL:\n{sql}\n\n"
        "Reply to this email with APPROVE or REJECT as the first line. "
        "You may add an optional comment after the decision. Only a reply from "
        "the configured approver is accepted.\n\n"
        f"Alternatively, review the request here:\n{url}\n\n"
        f"This request expires at {expiry_text}."
    )

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if use_tls:
                server.starttls()
            server.login(username, password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError(f"Could not send approval email: {exc}") from exc


def open_approval_inbox(
    host: str,
    port: int,
    username: str,
    password: str,
) -> imaplib.IMAP4_SSL:
    if not all((host, username, password)):
        raise ApprovalInboxError(
            "Email reply approval is not configured. Set IMAP_HOST and make sure "
            "IMAP_USERNAME/IMAP_PASSWORD (or SMTP_USERNAME/SMTP_PASSWORD) are configured."
        )
    try:
        mailbox = imaplib.IMAP4_SSL(host, port, timeout=15)
        mailbox.login(username, password)
        status, _ = mailbox.select("INBOX")
        if status != "OK":
            raise imaplib.IMAP4.error("Could not select the inbox.")
        return mailbox
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ApprovalInboxError(f"Could not open the approval inbox: {exc}") from exc


def _reply_text(message: Message) -> str:
    body_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != "text/plain" or part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if payload is not None:
            charset = message.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
        elif isinstance(message.get_payload(), str):
            body_parts.append(message.get_payload())
    return "\n".join(body_parts)


def parse_approval_reply(body: str) -> tuple[str, str] | None:
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith(">")
            or stripped.lower().startswith("on ") and stripped.lower().endswith("wrote:")
            or stripped == "-----Original Message-----"
        ):
            break
        lines.append(line.rstrip())

    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    match = re.fullmatch(r"\s*(APPROVE|REJECT)\s*(?::|-)?\s*(.*?)\s*", lines[0], re.IGNORECASE)
    if not match:
        return None
    decision = match.group(1).lower()
    comments = [match.group(2).strip()] if match.group(2).strip() else []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped == "--" or stripped.lower().startswith("sent from my "):
            break
        comments.append(stripped)
    comment = "\n".join(comments).strip()
    return decision, comment[:1000]


def find_approval_reply(
    mailbox: imaplib.IMAP4_SSL,
    request_id: str,
    approver_email: str,
    processed_uids: set[bytes] | None = None,
) -> ApprovalReply | None:
    processed = processed_uids if processed_uids is not None else set()
    try:
        status, data = mailbox.uid(
            "search", None, "ALL", "FROM", approver_email, "SUBJECT", request_id
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Could not search the approval inbox.")
        uids = data[0].split() if data and data[0] else []
        for uid in uids:
            if uid in processed:
                continue
            status, fetched = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw = next(
                (item[1] for item in fetched if isinstance(item, tuple) and isinstance(item[1], bytes)),
                None,
            )
            if raw is None:
                continue
            processed.add(uid)
            message = BytesParser(policy=policy.default).parsebytes(raw)
            sender = parseaddr(str(message.get("From", "")))[1].strip().lower()
            subject = str(make_header(decode_header(str(message.get("Subject", "")))))
            if sender != approver_email.strip().lower() or request_id not in subject:
                mailbox.uid("store", uid, "+FLAGS", r"(\Seen)")
                continue
            parsed = parse_approval_reply(_reply_text(message))
            mailbox.uid("store", uid, "+FLAGS", r"(\Seen)")
            if parsed is None:
                logger.info(
                    "Ignored reply without APPROVE or REJECT request_id=%s sender=%s",
                    request_id,
                    sender,
                )
                continue
            decision, comment = parsed
            return ApprovalReply(decision, sender, comment)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ApprovalInboxError(f"Could not read the approval inbox: {exc}") from exc
    return None


def close_approval_inbox(mailbox: imaplib.IMAP4_SSL | None) -> None:
    if mailbox is None:
        return
    try:
        mailbox.logout()
    except (OSError, imaplib.IMAP4.error):
        logger.debug("Approval inbox logout failed.", exc_info=True)
