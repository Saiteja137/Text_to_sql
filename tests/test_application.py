from __future__ import annotations

import asyncio
from email.message import EmailMessage
import sqlite3
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from text_to_sql.approvals import (
    ApprovalError,
    ApprovalReply,
    create_approval_request,
    decide_request,
    expire_approval_request,
    find_approval_reply,
    get_approval_request,
    initialize_approval_store,
    parse_approval_reply,
    send_approval_email,
)
from text_to_sql.app import create_app
from text_to_sql.database import initialize_database
from text_to_sql.langgraph_workflow import GenerationError, run_sql_workflow
from ollama import ResponseError

from text_to_sql.ollama_client import GeneratedQuery, OllamaClient, OllamaError
from text_to_sql.sql_safety import SqlValidationError, validate_read_only_sql
from text_to_sql.tracking import (
    TrackingAgent,
    create_tracking_run,
    get_tracking_run,
    initialize_tracking_store,
)


class FakeSMTP:
    def __init__(self) -> None:
        self.message = None

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def starttls(self) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        self.username = username

    def send_message(self, message: object) -> None:
        self.message = message


class FakeStreamingChain:
    def __init__(self, chunks: list[str], delay_seconds: float = 0) -> None:
        self.chunks = chunks
        self.delay_seconds = delay_seconds

    async def astream(self, _messages: object) -> AsyncIterator[str]:
        for chunk in self.chunks:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            yield chunk


class FakeIMAP:
    def __init__(self, raw_message: bytes) -> None:
        self.raw_message = raw_message
        self.calls: list[tuple[object, ...]] = []

    def uid(self, command: str, *args: object):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"101"]
        if command == "fetch":
            return "OK", [(b"101 (BODY[])", self.raw_message)]
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"LOGOUT completed"]


class FakeOllama:
    def __init__(
        self,
        sql_responses: list[str],
        evaluations: list[tuple[bool, str]],
        delay_seconds: float = 0,
    ) -> None:
        self.sql_responses = iter(sql_responses)
        self.evaluations = iter(evaluations)
        self.feedback_seen: list[str] = []
        self.call_count = 0
        self.delay_seconds = delay_seconds

    def generate_and_evaluate(
        self,
        question: str,
        schema: str,
        previous_sql: str = "",
        validation_feedback: str = "",
    ) -> GeneratedQuery:
        self.call_count += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.feedback_seen.append(validation_feedback)
        try:
            relevant, reason = next(self.evaluations)
        except StopIteration:
            relevant, reason = True, "The query is relevant."
        return GeneratedQuery(next(self.sql_responses), relevant, reason)


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "college.db"
        self.approval_path = self.root / "approvals.db"
        initialize_database(self.database_path)
        initialize_approval_store(self.approval_path)
        initialize_tracking_store(self.approval_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_seed_is_idempotent_and_preserves_existing_data(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("UPDATE employees SET salary = 123456 WHERE employee_id = 1")
        initialize_database(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT salary FROM employees WHERE employee_id = 1"
                ).fetchone()[0],
                123456,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM students").fetchone()[0], 10
            )

    def test_validator_rejects_mutation_multiple_statements_and_invalid_tables(self) -> None:
        validate_read_only_sql(
            "SELECT AVG(e.salary) FROM employees e "
            "JOIN departments d ON d.department_id = e.department_id "
            "WHERE d.department_name = 'Artificial Intelligence'",
            self.database_path,
        )
        for sql in (
            "DELETE FROM employees",
            "SELECT 1; SELECT 2",
            "SELECT * FROM missing_table",
            "PRAGMA table_info(employees)",
        ):
            with self.subTest(sql=sql), self.assertRaises(SqlValidationError):
                validate_read_only_sql(sql, self.database_path)

    def test_missing_ollama_model_reports_actionable_error(self) -> None:
        fake_chat = MagicMock()
        fake_chat.__or__.return_value.astream.side_effect = ResponseError(
            "model 'llama3.1:8b' not found", status_code=404
        )
        with patch("text_to_sql.ollama_client.ChatOllama", return_value=fake_chat):
            client = OllamaClient("http://localhost:11434", "llama3.1:8b")
            with self.assertRaisesRegex(OllamaError, "ollama pull <model>"):
                client.generate_and_evaluate("Show employees", "employees schema")

    def test_ollama_memory_error_recommends_a_smaller_model(self) -> None:
        fake_chat = MagicMock()
        fake_chat.__or__.return_value.astream.side_effect = ResponseError(
            "error loading model: unable to allocate CPU buffer", status_code=500
        )
        with patch("text_to_sql.ollama_client.ChatOllama", return_value=fake_chat):
            client = OllamaClient("http://localhost:11434", "llama3.1:8b")
            with self.assertRaisesRegex(
                OllamaError, "pulling it again will not free memory"
            ):
                client.generate_and_evaluate("Show employees", "employees schema")

    def test_generation_returns_sql_and_relevance_in_one_ollama_call(self) -> None:
        fake_chat = MagicMock()
        fake_chain = FakeStreamingChain(
            [
                '{"sql":"SELECT COUNT(*) FROM employees",',
                '"relevant":true,',
                '"reason":"Counts employees."}',
            ]
        )
        fake_chat.__or__.return_value = fake_chain
        with patch("text_to_sql.ollama_client.ChatOllama", return_value=fake_chat):
            client = OllamaClient("http://localhost:11434", "llama3.1:8b")
            generated = client.generate_and_evaluate(
                "How many employees are there?", "employees schema"
            )
        self.assertEqual(generated.sql, "SELECT COUNT(*) FROM employees")
        self.assertTrue(generated.relevant)

    def test_ollama_request_has_a_hard_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 120"):
            OllamaClient(
                "http://localhost:11434", "llama3.1:8b", timeout_seconds=121
            )
        fake_chat = MagicMock()
        fake_chat.__or__.return_value = FakeStreamingChain(["late response"], 2)
        with patch("text_to_sql.ollama_client.ChatOllama", return_value=fake_chat):
            client = OllamaClient(
                "http://localhost:11434", "llama3.1:8b", timeout_seconds=1
            )
            with self.assertRaisesRegex(OllamaError, "exceeded its 1-second limit"):
                client._invoke("system", "user", operation="test")

    def test_langgraph_retries_semantic_evaluation_failure(self) -> None:
        run_id = "semantic-retry"
        create_tracking_run(self.approval_path, run_id)
        client = FakeOllama(
            ["SELECT COUNT(*) FROM students", "SELECT AVG(salary) FROM employees"],
            [
                (False, "The question asks about employee salary."),
                (True, "Calculates employee average salary."),
            ],
        )
        sql, attempts, schema = run_sql_workflow(
            question="What is the average employee salary?",
            database_path=str(self.database_path),
            max_attempts=3,
            query_timeout_seconds=5,
            client=client,  # type: ignore[arg-type]
            tracker=TrackingAgent(self.approval_path, run_id),
        )
        self.assertIn("AVG", sql)
        self.assertEqual(attempts, 2)
        self.assertIn("employees", schema)
        self.assertIn("employee salary", client.feedback_seen[1])
        run = get_tracking_run(self.approval_path, run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["attempts"], 2)
        self.assertEqual(client.call_count, 2)
        self.assertEqual(client.call_count, 2)
        self.assertIn("retrying", [event["status"] for event in run["events"]])

    def test_langgraph_enforces_three_attempt_limit(self) -> None:
        run_id = "retry-limit"
        create_tracking_run(self.approval_path, run_id)
        client = FakeOllama(["DELETE FROM employees"] * 3, [])
        with self.assertRaisesRegex(GenerationError, "after 3 attempt"):
            run_sql_workflow(
                question="Show employees",
                database_path=str(self.database_path),
                max_attempts=3,
                query_timeout_seconds=5,
                client=client,  # type: ignore[arg-type]
                tracker=TrackingAgent(self.approval_path, run_id),
            )
        self.assertEqual(len(client.feedback_seen), 3)
        self.assertEqual(client.call_count, 3)
        run = get_tracking_run(self.approval_path, run_id)
        assert run is not None
        self.assertEqual(run["status"], "failed")

    def test_approval_is_required_before_query_results_are_read(self) -> None:
        sql = "SELECT employee_id, salary FROM employees WHERE employee_id = 1"
        request_id, token = create_approval_request(
            self.approval_path,
            "Show employee salary",
            sql,
            attempts=1,
            now=time.time(),
            expiry_seconds=1200,
        )
        request = get_approval_request(
            self.approval_path, request_id, token, time.time()
        )
        self.assertEqual(request["status"], "pending")
        result = decide_request(
            self.approval_path,
            self.database_path,
            request_id,
            token,
            "approve",
            time.time(),
            5,
            200,
            decision_comment="Reviewed.",
            decided_by="approver@example.com",
        )
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["results"]["rows"], [[1, 128000.0]])
        self.assertEqual(result["decision_comment"], "Reviewed.")
        self.assertEqual(result["decided_by"], "approver@example.com")

    def test_rejection_stores_comment_without_executing_query(self) -> None:
        request_id, token = create_approval_request(
            self.approval_path,
            "Show employee salary",
            "SELECT salary FROM employees",
            attempts=1,
            now=time.time(),
            expiry_seconds=1200,
        )
        result = decide_request(
            self.approval_path,
            self.database_path,
            request_id,
            token,
            "reject",
            time.time(),
            5,
            200,
            decision_comment="Wrong data requested.",
            decided_by="approver@example.com",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["decision_comment"], "Wrong data requested.")
        self.assertNotIn("results", result)

    def test_expired_request_cannot_be_approved(self) -> None:
        request_id, token = create_approval_request(
            self.approval_path,
            "Show students",
            "SELECT student_id FROM students",
            attempts=1,
            now=100,
            expiry_seconds=1200,
        )
        with self.assertRaisesRegex(ApprovalError, "expired"):
            decide_request(
                self.approval_path,
                self.database_path,
                request_id,
                token,
                "approve",
                1301,
                5,
                200,
            )

    def test_expiration_is_persisted_without_executing_query(self) -> None:
        request_id, _ = create_approval_request(
            self.approval_path,
            "Show students",
            "SELECT student_id FROM students",
            attempts=1,
            now=100,
            expiry_seconds=1200,
        )
        self.assertTrue(expire_approval_request(self.approval_path, request_id, 1300))
        with closing(sqlite3.connect(self.approval_path)) as connection, connection:
            status = connection.execute(
                "SELECT status FROM approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()[0]
        self.assertEqual(status, "expired")

    def test_reply_parser_accepts_approve_or_reject_and_optional_comment(self) -> None:
        self.assertEqual(parse_approval_reply("APPROVE\nLooks good."), ("approve", "Looks good."))
        self.assertEqual(parse_approval_reply("REJECT: wrong department"), ("reject", "wrong department"))
        self.assertEqual(parse_approval_reply("APPROVE"), ("approve", ""))
        self.assertIsNone(parse_approval_reply("I will review this later."))
        self.assertEqual(
            parse_approval_reply("APPROVE\nLooks good.\n\n> old quoted reply"),
            ("approve", "Looks good."),
        )

    def test_inbox_reply_is_correlated_and_sender_verified(self) -> None:
        message = EmailMessage()
        message["From"] = "saitejaatkapuram7@gmail.com"
        message["Subject"] = "[Text-to-SQL approval request-id] Please approve"
        message.set_content("APPROVE\nReviewed and safe.")
        inbox = FakeIMAP(message.as_bytes())
        reply = find_approval_reply(inbox, "request-id", "saitejaatkapuram7@gmail.com")
        self.assertEqual(
            reply,
            ApprovalReply("approve", "saitejaatkapuram7@gmail.com", "Reviewed and safe."),
        )
        self.assertEqual([call[0] for call in inbox.calls], ["search", "fetch", "store"])

    def test_inbox_ignores_reply_from_wrong_sender(self) -> None:
        message = EmailMessage()
        message["From"] = "someone-else@example.com"
        message["Subject"] = "[Text-to-SQL approval request-id]"
        message.set_content("APPROVE")
        inbox = FakeIMAP(message.as_bytes())
        self.assertIsNone(find_approval_reply(inbox, "request-id", "approver@example.com"))

    def test_approval_email_uses_configured_sender_and_recipient(self) -> None:
        smtp = FakeSMTP()
        with patch("text_to_sql.approvals.smtplib.SMTP", return_value=smtp):
            send_approval_email(
                host="smtp.example.edu",
                port=587,
                username="sender@example.edu",
                password="test-password",
                use_tls=True,
                sender="sender@example.edu",
                recipient="saitejaatkapuram7@gmail.com",
                base_url="http://localhost:5173/approval",
                request_id="request-id",
                token="private-token",
                question="How many employees are there?",
                sql="SELECT COUNT(*) FROM employees",
                expires_at=time.time() + 1200,
            )
        self.assertIsNotNone(smtp.message)
        assert smtp.message is not None
        self.assertEqual(smtp.message["From"], "sender@example.edu")
        self.assertEqual(smtp.message["To"], "saitejaatkapuram7@gmail.com")
        self.assertIn("APPROVE or REJECT", smtp.message.get_content())
        self.assertIn("request_id=request-id", smtp.message.get_content())
        self.assertIn("private-token", smtp.message.get_content())
        self.assertNotIn("From:", smtp.message.get_content())
        self.assertNotIn("Sent to approver:", smtp.message.get_content())

    def test_fastapi_tracking_and_approval_endpoints(self) -> None:
        test_app = create_app(
            {
                "DATABASE_PATH": self.database_path,
                "APPROVAL_DB_PATH": self.approval_path,
                "MAX_GENERATION_ATTEMPTS": 3,
                "APPROVAL_EXPIRY_MINUTES": 20,
                "APPROVAL_REQUIRED": True,
                "QUERY_TIMEOUT_SECONDS": 5,
                "MAX_RESULT_ROWS": 200,
                "APPROVER_EMAIL": "saitejaatkapuram7@gmail.com",
                "SMTP_HOST": "smtp.example.edu",
                "SMTP_PORT": 587,
                "SMTP_USERNAME": "sender",
                "SMTP_PASSWORD": "password",
                "SMTP_USE_TLS": True,
                "MAIL_FROM": "sender@example.edu",
                "APPROVAL_BASE_URL": "http://localhost:5173/approval",
                "CORS_ORIGINS": ["http://localhost:5173"],
            }
        )
        sql = "SELECT COUNT(*) AS employee_count FROM employees"
        client = FakeOllama([sql], [(True, "Counts employees.")])
        emailed: dict[str, object] = {}

        def capture_email(**kwargs: object) -> None:
            emailed.update(kwargs)

        with (
            patch("text_to_sql.app.OllamaClient", return_value=client),
            patch("text_to_sql.app.send_approval_email", side_effect=capture_email),
            patch("text_to_sql.app.open_approval_inbox", return_value=FakeIMAP(b"")),
            patch("text_to_sql.app.find_approval_reply", return_value=None),
            TestClient(test_app) as web,
        ):
            self.assertEqual(web.get("/").status_code, 200)
            self.assertEqual(web.get("/approval?request_id=sample&token=sample").status_code, 200)
            response = web.post(
                "/api/query",
                json={"question": "How many employees are there?"},
            )
            self.assertEqual(response.status_code, 202)
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + 10
            run = web.get(f"/api/runs/{run_id}").json()
            while (
                run["current_stage"] != "approval_poll"
                and run["status"] not in {"failed", "email_failed"}
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                run = web.get(f"/api/runs/{run_id}").json()

            self.assertEqual(run["current_stage"], "approval_poll")
            self.assertEqual(client.call_count, 1)
            self.assertEqual(emailed["recipient"], "saitejaatkapuram7@gmail.com")
            self.assertIn("request_id", emailed)
            self.assertIn("token", emailed)
            request_id = str(emailed["request_id"])
            token = str(emailed["token"])
            details = web.get(f"/api/approval/{request_id}/{token}")
            self.assertEqual(details.status_code, 200)
            self.assertEqual(details.json()["status"], "pending")
            self.assertIsNone(details.json()["results"])

            approved = web.post(
                f"/api/approval/{request_id}/{token}",
                json={"decision": "approve"},
            )
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["results"]["rows"], [[10]])
            self.assertEqual(web.get(f"/api/runs/{run_id}").json()["status"], "approved")
            details_after_approval = web.get(f"/api/approval/{request_id}/{token}").json()
            self.assertEqual(details_after_approval["results"]["rows"], [[10]])
            self.assertEqual(details_after_approval["decision_comment"], "")
            event_stream = web.get(f"/api/runs/{run_id}/events")
            self.assertEqual(event_stream.status_code, 200)
            self.assertIn("execution", event_stream.text)
            self.assertIn("SELECT COUNT(*) AS employee_count FROM employees", event_stream.text)

    def test_email_approval_reply_executes_and_tracks_approver_comment(self) -> None:
        test_app = create_app(
            {
                "DATABASE_PATH": self.database_path,
                "APPROVAL_DB_PATH": self.approval_path,
                "MAX_GENERATION_ATTEMPTS": 3,
                "APPROVAL_EXPIRY_MINUTES": 20,
                "APPROVAL_REQUIRED": True,
                "QUERY_TIMEOUT_SECONDS": 5,
                "MAX_RESULT_ROWS": 200,
                "APPROVER_EMAIL": "saitejaatkapuram7@gmail.com",
                "SMTP_HOST": "smtp.example.edu",
                "SMTP_PORT": 587,
                "SMTP_USERNAME": "sender",
                "SMTP_PASSWORD": "password",
                "SMTP_USE_TLS": True,
                "MAIL_FROM": "sender@example.edu",
                "APPROVAL_BASE_URL": "http://localhost:5173/approval",
                "IMAP_HOST": "imap.example.edu",
                "IMAP_PORT": 993,
                "IMAP_USERNAME": "sender",
                "IMAP_PASSWORD": "password",
                "IMAP_POLL_INTERVAL_SECONDS": 15,
                "CORS_ORIGINS": ["http://localhost:5173"],
            }
        )
        client = FakeOllama(
            ["SELECT COUNT(*) AS employee_count FROM employees"],
            [(True, "Counts employees.")],
        )
        with (
            patch("text_to_sql.app.OllamaClient", return_value=client),
            patch("text_to_sql.app.send_approval_email"),
            patch("text_to_sql.app.open_approval_inbox", return_value=FakeIMAP(b"")),
            patch(
                "text_to_sql.app.find_approval_reply",
                return_value=ApprovalReply(
                    "approve",
                    "saitejaatkapuram7@gmail.com",
                    "Reviewed and approved.",
                ),
            ),
            TestClient(test_app) as web,
        ):
            response = web.post("/api/query", json={"question": "Count employees"})
            self.assertEqual(response.status_code, 202)
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + 10
            run = web.get(f"/api/runs/{run_id}").json()
            while run["status"] not in {"approved", "failed"} and time.monotonic() < deadline:
                time.sleep(0.05)
                run = web.get(f"/api/runs/{run_id}").json()

            self.assertEqual(run["status"], "approved", run)
            self.assertEqual(run["results"]["rows"], [[10]])
            execution = next(
                event
                for event in run["events"]
                if event["stage"] == "execution" and event["status"] == "completed"
            )
            self.assertEqual(
                execution["metadata"]["sql"],
                "SELECT COUNT(*) AS employee_count FROM employees",
            )
            self.assertIn("Reviewed and approved.", execution["detail"])
            approval = next(event for event in run["events"] if event["stage"] == "approval")
            self.assertIn("Approved by saitejaatkapuram7@gmail.com", approval["detail"])

    def test_approval_disabled_executes_read_only_without_email(self) -> None:
        test_app = create_app(
            {
                "DATABASE_PATH": self.database_path,
                "APPROVAL_DB_PATH": self.approval_path,
                "MAX_GENERATION_ATTEMPTS": 3,
                "APPROVAL_REQUIRED": False,
                "QUERY_TIMEOUT_SECONDS": 5,
                "MAX_RESULT_ROWS": 200,
                "CORS_ORIGINS": ["http://localhost:5173"],
            }
        )
        client = FakeOllama(
            ["SELECT COUNT(*) AS employee_count FROM employees"],
            [(True, "Counts employees.")],
        )
        with (
            patch("text_to_sql.app.OllamaClient", return_value=client),
            patch(
                "text_to_sql.app.send_approval_email",
                side_effect=AssertionError("email must not be sent when approval is disabled"),
            ) as send_email,
            TestClient(test_app) as web,
        ):
            self.assertFalse(web.get("/api/config").json()["approval_required"])
            response = web.post("/api/query", json={"question": "How many employees are there?"})
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + 10
            run = web.get(f"/api/runs/{run_id}").json()
            while run["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
                time.sleep(0.05)
                run = web.get(f"/api/runs/{run_id}").json()
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual(run["results"]["columns"], ["employee_count"])
            self.assertEqual(run["results"]["rows"], [[10]])
            send_email.assert_not_called()
            with closing(sqlite3.connect(self.approval_path)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM approval_requests"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_api_rejects_concurrent_query_submissions(self) -> None:
        test_app = create_app(
            {
                "DATABASE_PATH": self.database_path,
                "APPROVAL_DB_PATH": self.approval_path,
                "MAX_GENERATION_ATTEMPTS": 3,
                "APPROVAL_REQUIRED": False,
                "QUERY_TIMEOUT_SECONDS": 5,
                "MAX_RESULT_ROWS": 200,
                "CORS_ORIGINS": ["http://localhost:5173"],
            }
        )
        client = FakeOllama(
            ["SELECT COUNT(*) FROM employees"],
            [(True, "Counts employees.")],
            delay_seconds=0.25,
        )
        with patch("text_to_sql.app.OllamaClient", return_value=client), TestClient(test_app) as web:
            first = web.post("/api/query", json={"question": "Count employees"})
            self.assertEqual(first.status_code, 202)
            second = web.post("/api/query", json={"question": "Count students"})
            self.assertEqual(second.status_code, 429)
            self.assertIn("already processing", second.json()["detail"])

            run_id = first.json()["run_id"]
            deadline = time.monotonic() + 10
            run = web.get(f"/api/runs/{run_id}").json()
            while run["status"] not in {"completed", "failed"} and time.monotonic() < deadline:
                time.sleep(0.05)
                run = web.get(f"/api/runs/{run_id}").json()
            self.assertEqual(run["status"], "completed", run)
            self.assertEqual(client.call_count, 1)


if __name__ == "__main__":
    unittest.main()
