import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

type WorkflowEvent = {
  id: number;
  stage: string;
  status: string;
  detail: string;
  attempt: number | null;
  created_at: number;
};

type WorkflowRun = {
  run_id: string;
  status: string;
  current_stage: string;
  attempts: number;
  sql: string | null;
  results: QueryResults | null;
  error: string | null;
  events: WorkflowEvent[];
};

type QueryResults = {
  columns: string[];
  rows: (string | number | null)[][];
  truncated: boolean;
};

type ApprovalRequest = {
  request_id: string;
  question: string;
  sql: string;
  attempts: number;
  expires_at: number;
  status: string;
  results: QueryResults | null;
  decision_comment: string | null;
  decided_by: string | null;
};

const terminalStatuses = new Set(["completed", "approved", "rejected", "expired", "failed", "email_failed"]);
const stageNames: Record<string, string> = {
  queued: "Queued",
  schema: "Read database schema",
  generation: "Generate SQL",
  validation: "Validate SQL",
  evaluation: "Evaluate relevance",
  workflow: "Complete workflow",
  approval_email: "Send approval email",
  approval_poll: "Wait for email reply",
  approval: "Human decision",
  execution: "Execute read-only query",
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

function ApprovalPage() {
  const params = new URLSearchParams(window.location.search);
  const requestId = params.get("request_id") ?? "";
  const token = params.get("token") ?? "";
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [comment, setComment] = useState("");

  const loadApproval = useCallback(async () => {
    try {
      setApproval(
        await api<ApprovalRequest>(
          `/api/approval/${encodeURIComponent(requestId)}/${encodeURIComponent(token)}`,
        ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load approval.");
    }
  }, [requestId, token]);

  useEffect(() => {
    if (!requestId || !token) {
      setError("This approval link is missing its request or security token.");
      return;
    }
    void loadApproval();
  }, [loadApproval, requestId, token]);

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError("");
    try {
      const result = await api<{
        status: string;
        question: string;
        sql: string;
        results: QueryResults | null;
        decision_comment: string | null;
        decided_by: string | null;
      }>(`/api/approval/${encodeURIComponent(requestId)}/${encodeURIComponent(token)}`, {
        method: "POST",
        body: JSON.stringify({ decision, comment }),
      });
      setApproval((current) =>
        current
          ? {
              ...current,
              status: result.status,
              results: result.results,
              decision_comment: result.decision_comment,
              decided_by: result.decided_by,
            }
          : current,
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The decision could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="shell narrow">
      <a className="brand" href="/">COLLEGE DATA <span>ASSISTANT</span></a>
      <p className="eyebrow">HUMAN REVIEW</p>
      <h1>Review this query</h1>
      <p className="intro">Opening this page will not execute the query. Only choosing Approve runs it.</p>
      {error && <div className="notice error">{error}</div>}
      {approval && (
        <section className="panel">
          <div className="statusline"><span className={`dot ${approval.status}`} />{approval.status.replaceAll("_", " ")}</div>
          <h2>Question</h2>
          <p>{approval.question}</p>
          <h2>Read-only SQL</h2>
          <pre>{approval.sql}</pre>
          <p className="subtle">
            Passed validation in {approval.attempts} attempt(s). Expires{" "}
            {new Date(approval.expires_at * 1000).toLocaleString()}.
          </p>
          {approval.status === "pending" && (
            <>
              <label htmlFor="decision-comment">Optional comment</label>
              <textarea
                id="decision-comment"
                maxLength={1000}
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="Add a comment for the workflow tracker"
              />
              <div className="actions">
                <button disabled={busy} onClick={() => void decide("approve")}>
                  {busy ? "Saving…" : "Approve and execute"}
                </button>
                <button className="button secondary" disabled={busy} onClick={() => void decide("reject")}>
                  Reject
                </button>
              </div>
            </>
          )}
          {approval.status === "approved" && (
            <div className="notice success">
              Approved by {approval.decided_by ?? "the configured approver"}. Comment:{" "}
              {approval.decision_comment || "No comment provided."}
            </div>
          )}
          {approval.status === "expired" && <div className="notice">Approval expired. The query was not executed.</div>}
          {approval.results && <Results results={approval.results} />}
          {approval.status === "rejected" && (
            <div className="notice">
              Rejected by {approval.decided_by ?? "the configured approver"}. Comment:{" "}
              {approval.decision_comment || "No comment provided."} The query was not executed.
            </div>
          )}
        </section>
      )}
    </main>
  );
}

function Results({ results }: { results: QueryResults }) {
  return (
    <section className="results">
      <h2>Query results</h2>
      {results.truncated && <div className="notice">Showing the first {results.rows.length} rows.</div>}
      <div className="table-scroll">
        <table>
          <thead><tr>{results.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
          <tbody>
            {results.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>{row.map((value, columnIndex) => <td key={columnIndex}>{value ?? "NULL"}</td>)}</tr>
            ))}
            {results.rows.length === 0 && (
              <tr><td colSpan={Math.max(results.columns.length, 1)}>No rows returned.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Dashboard() {
  const [question, setQuestion] = useState("");
  const [runId, setRunId] = useState("");
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [schema, setSchema] = useState("");
  const [approvalRequired, setApprovalRequired] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const workflowRunning = Boolean(runId) && (!run || !terminalStatuses.has(run.status));

  useEffect(() => {
    void api<{ schema: string }>("/api/schema")
      .then((response) => setSchema(response.schema))
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Schema unavailable."));
    void api<{ approval_required: boolean }>("/api/config")
      .then((response) => setApprovalRequired(response.approval_required))
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Configuration unavailable."));
  }, []);

  useEffect(() => {
    if (!runId) return;
    const stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
    stream.onmessage = (event) => {
      try {
        const nextRun = JSON.parse(event.data) as WorkflowRun;
        setError("");
        setRun(nextRun);
        if (terminalStatuses.has(nextRun.status)) stream.close();
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Could not read workflow status.");
        stream.close();
      }
    };
    stream.onerror = () => {
      setError("Tracker connection interrupted; reconnecting.");
    };
    return () => {
      stream.close();
    };
  }, [runId]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    setRun(null);
    try {
      const response = await api<{ run_id: string }>("/api/query", {
        method: "POST",
        body: JSON.stringify({ question }),
      });
      setRunId(response.run_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start workflow.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="/">COLLEGE DATA <span>ASSISTANT</span></a>
        <span className="local-badge"><i /> {approvalRequired === null ? "Loading mode…" : approvalRequired ? "Human approval on" : "Direct read-only mode"}</span>
      </header>
      <section className="hero">
        <p className="eyebrow">ASK YOUR COLLEGE DATABASE</p>
        <h1>From a question<br />to a <em>carefully reviewed</em> answer.</h1>
        <p className="intro">LangGraph coordinates generation, SQL safety checks, and relevance evaluation. Your database stays read-only. {approvalRequired === null ? "Checking execution mode…" : approvalRequired ? "Validated queries are emailed for approval before execution." : "Validated queries execute directly without email approval."}</p>
      </section>
      <div className="layout">
        <section className="panel ask-panel">
          <div className="panel-heading"><span className="step">01</span><div><h2>Ask a question</h2><p>Be specific about filters and calculations.</p></div></div>
          <form onSubmit={(event) => void submit(event)}>
            <label htmlFor="question">Your question</label>
            <textarea id="question" maxLength={1000} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What is the average salary of employees in the Artificial Intelligence department?" required />
            <button disabled={submitting || workflowRunning || !question.trim()}>{submitting ? "Starting…" : workflowRunning ? "Query in progress…" : "Start query workflow"} <span aria-hidden="true">→</span></button>
          </form>
          {error && <div className="notice error">{error}</div>}
        </section>
        <section className="panel tracker-panel">
          <div className="panel-heading"><span className="step">02</span><div><h2>Live process tracker</h2><p>Follow each agent and workflow step.</p></div></div>
          {!run && <p className="empty-state">Submit a question to watch schema discovery, SQL generation, validation, evaluation, and approval.</p>}
          {run && (
            <>
              <div className="run-status"><span className={`dot ${run.status}`} /><strong>{stageNames[run.current_stage] ?? run.current_stage}</strong><span className="subtle">{run.status.replaceAll("_", " ")}</span></div>
              <ol className="timeline">
                {run.events.map((item) => (
                  <li key={item.id} className={item.status}>
                    <span className="timeline-icon">{item.status === "failed" ? "!" : item.status === "running" ? "…" : "✓"}</span>
                    <div><div className="event-title">{stageNames[item.stage] ?? item.stage}{item.attempt ? ` · attempt ${item.attempt}` : ""}</div><p>{item.detail}</p><time>{new Date(item.created_at * 1000).toLocaleTimeString()}</time></div>
                  </li>
                ))}
              </ol>
              {run.sql && (
                <>
                  <h3>
                    {run.events.some((item) => item.stage === "execution" && item.status === "completed")
                      ? "Executed SQL (read-only)"
                      : "Generated SQL (read-only)"}
                  </h3>
                  <pre>{run.sql}</pre>
                </>
              )}
              {run.error && <div className="notice error">{run.error}</div>}
              {run.current_stage === "approval_poll" && (
                <div className="notice success">
                  Approval email sent. Waiting for the approver’s email reply. The query has not run.
                </div>
              )}
              {run.status === "completed" && run.results && (
                <><div className="notice success">Executed read-only. Email approval is disabled.</div><Results results={run.results} /></>
              )}
            </>
          )}
        </section>
      </div>
      <details className="panel schema-panel"><summary>Explore database schema</summary><pre>{schema || "Loading schema…"}</pre></details>
      <footer>FastAPI · React + TypeScript · LangChain · LangGraph · Ollama · SQLite</footer>
    </main>
  );
}

export default function App() {
  return window.location.pathname.startsWith("/approval") ? <ApprovalPage /> : <Dashboard />;
}
