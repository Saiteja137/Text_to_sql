# College Text-to-SQL

A full-stack natural-language-to-SQL demo with a **FastAPI** backend, **React + TypeScript** frontend, **LangChain + LangGraph** orchestration, **Ollama**, and a sample **SQLite** college database.

The workflow reads the current schema, generates one SQLite `SELECT`, checks it using SQL parsing and SQLite's read-only authorizer, and asks the LLM to evaluate whether it answers the question. LangGraph retries validation/evaluation failures at most three times. A tracking agent records each step in SQLite so the UI can display live progress. A valid query is emailed to a human reviewer and **is not executed until that reviewer explicitly approves it**. Approval links expire after 20 minutes.

The sample data is synthetic. Tables include `departments`, `employees`, `students`, `courses`, and `enrollments`.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.10+
- Node.js 20+ and npm
- [Ollama](https://ollama.com/) installed and running locally
- An SMTP-enabled sender account

## 1. Python environment and backend

From the project directory in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
python -m text_to_sql.init_db
```

If PowerShell prevents venv activation, run commands using `.\.venv\Scripts\python.exe`.

## 2. Install and start Ollama

```powershell
ollama pull llama3.2:1b
ollama serve
```

Keep the Ollama server running. The low-memory default is `llama3.2:1b`. If you
already have a local `.env`, update its `OLLAMA_MODEL` value too; `.env` overrides
the default in the code.

## 3. Configure approval email

Create the local environment file and edit it:

```powershell
Copy-Item .env.example .env
```

The configured approver is `saitejaatkapuram7@gmail.com`. **An AI agent does not have an email account or sender mailbox.** Configure an SMTP sender account that you own or are authorized to use:

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-sender@gmail.com
SMTP_PASSWORD=your-google-app-password
SMTP_USE_TLS=true
MAIL_FROM=your-sender@gmail.com
APPROVER_EMAIL=saitejaatkapuram7@gmail.com
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your-sender@gmail.com
IMAP_PASSWORD=your-google-app-password
IMAP_POLL_INTERVAL_SECONDS=15
```

For Gmail, use an app password on an account with 2-Step Verification; do not use your regular Gmail password. `.env` is ignored by Git. Never commit real email credentials.

**SMTP** sends outgoing email; **IMAP** reads incoming replies. For Gmail, use `smtp.gmail.com:587` with TLS and `imap.gmail.com:993` with SSL. IMAP must be enabled for the sender mailbox. Set `IMAP_USERNAME` and `IMAP_PASSWORD` to the sender mailbox credentials (they may be the same app password as SMTP). Replies go to the sender mailbox; the application accepts only replies from `APPROVER_EMAIL` with the request ID in the subject. Do not send passwords in chat or commit them; enter them only in your local, ignored `.env`.

`APPROVAL_BASE_URL` should point to the React app's `/approval` page. The default is `http://localhost:5173/approval`, which works when the approver opens the email on the same computer. For remote approval, deploy both services behind HTTPS at reachable URLs, set `APPROVAL_BASE_URL` to the frontend URL, and update `CORS_ORIGINS` to the deployed frontend origin. An approval link is a bearer secret; anyone with it can approve or reject that request.

## 4. Start the application

Create a `.env` first. The launcher starts both app services; Ollama must also be running separately.

**Terminal 1 — Ollama** (if not already running):

```powershell
ollama serve
```

**Terminal 2 — FastAPI + React:**

Install frontend packages once:

```bash
npm --prefix frontend install
```

On Linux/macOS, WSL, or Git Bash:

```bash
chmod +x run.sh
./run.sh
```

`run.sh` starts the API and Vite frontend together and stops both when you press Ctrl+C. On native Windows PowerShell, start both services in one window with:

```powershell
.\run.ps1
```

The PowerShell launcher keeps both processes attached to the same terminal, so Uvicorn request logs, workflow stage logs, model/email errors, and Vite output are visible there. Press Ctrl+C to stop both services.

If you prefer to run the services separately, use two PowerShell terminals:

**Terminal 1 — FastAPI:**

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn --app-dir .\src text_to_sql.app:app --reload --host 127.0.0.1 --port 8000
```

**Terminal 2 — React:**

```powershell
cd frontend
npm install
npm run dev
```

API health and interactive API docs: <http://127.0.0.1:8000/api/health> and <http://127.0.0.1:8000/docs>.

Open <http://localhost:5173>. Ask, for example:

> What is the average salary of employees in the Artificial Intelligence department?

The live process tracker shows schema discovery, each SQL generation attempt, validation, relevance evaluation, and approval email status. The email contains the question and generated SQL. Reply with `APPROVE` or `REJECT` on the first line; optional following text is saved as a comment. Replies are checked every 15 seconds for up to 20 minutes. Only an `APPROVE` from the configured approver executes the validated query read-only. `REJECT` and expiration do not execute it. The original approval link remains available as an alternative.

For a step-by-step explanation of the **actual working flow**, including the retry, approval, rejection, and expiry paths, see [docs/flowdiagram.md](docs/flowdiagram.md).

For the short, user-facing view of **what happens in the frontend**, see [docs/frontend-flow.md](docs/frontend-flow.md).

## Production frontend build

Build the static React app:

```powershell
cd frontend
npm install
npm run build
cd ..
```

The FastAPI server serves `frontend/dist` at `/` and `/approval` after the build. Run the backend using a production ASGI server configuration appropriate for your deployment. Do not expose a development server directly to the public internet.

## Workflow and safety

1. **Schema:** read SQLite tables, columns, and foreign-key relationships.
2. **Generation and relevance self-check:** one LangChain `ChatOllama` request returns the SQL and its relevance assessment together.
3. **Validation:** SQLGlot checks the statement shape. SQLite prepares an `EXPLAIN QUERY PLAN` on a `mode=ro`, `query_only` connection with an authorizer. This does not fetch result rows.
4. **Retry:** validation or relevance failures feed back into the next LangGraph attempt, capped at three total model requests per question. Output is capped at 256 tokens.
5. **Tracking:** a tracking agent persists timestamped workflow events and run status in `data/approvals.db`; the browser uses one Server-Sent Events connection rather than repeated status requests.
6. **Approval:** the query is emailed to the configured approver. The app watches the sender inbox over IMAP for a matching, authenticated approver reply. Email approvals expire in 20 minutes.
7. **Execution:** only an explicit approval POST runs the saved query. Execution revalidates the SQL and uses a read-only SQLite connection, a progress timeout, and a result-row cap.

Read-only URI mode, SQLite's authorizer, SQL parsing, and explicit human approval are separate safeguards. The query workflow never writes to the college database. LLM relevance evaluation is not a guarantee of factual correctness; review the requested question and SQL carefully. This starter app does not include user accounts, enterprise authorization, or production secrets management.

## Configuration

See `.env.example` for all settings.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_PATH` | `data/college.db` | College SQLite database |
| `APPROVAL_DB_PATH` | `data/approvals.db` | Approval requests and tracking events |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `OLLAMA_MODEL` | `llama3.2:1b` | Model used for SQL generation and relevance self-check |
| `OLLAMA_TIMEOUT_SECONDS` | `120` | Hard per-call timeout in seconds (1–120) |
| `MAX_GENERATION_ATTEMPTS` | `3` | Maximum total Ollama requests for one question (1–3) |
| `QUERY_TIMEOUT_SECONDS` | `5` | SQLite query timeout |
| `MAX_RESULT_ROWS` | `200` | Maximum result rows returned |
| `APPROVAL_EXPIRY_MINUTES` | `20` | Approval link lifetime (1–20) |
| `IMAP_HOST` | `imap.gmail.com` | Incoming approval-reply mailbox |
| `IMAP_PORT` | `993` | IMAP over SSL port |
| `IMAP_USERNAME` | Sender mailbox | Mailbox used to receive approver replies |
| `IMAP_PASSWORD` | Sender app password | IMAP credential; may match `SMTP_PASSWORD` |
| `IMAP_POLL_INTERVAL_SECONDS` | `15` | Interval between inbox checks (5–60 seconds) |
| `MAIL_FROM` | — | Sender address from your SMTP account |
| `APPROVER_EMAIL` | `saitejaatkapuram7@gmail.com` | Human approver recipient |
| `APPROVAL_BASE_URL` | `http://localhost:5173/approval` | Frontend approval URL |
| `CORS_ORIGINS` | Local Vite origins | Comma-separated frontend origins |

## Tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked Ollama/SMTP services; they do not send email or require a running Ollama server.

## Troubleshooting Ollama

If the terminal says a model is not found, start Ollama and pull the model configured in `.env`:

```powershell
ollama serve
ollama pull llama3.2:1b
ollama list
```

Set `OLLAMA_MODEL` in `.env` to exactly match a name shown by `ollama list`, then restart `.\run.ps1`.

If the log contains `unable to allocate CPU buffer` or `failed to allocate`,
Ollama found the model but could not allocate enough system memory to load it.
Pulling a model that is already installed will not fix a memory shortage. Close
memory-intensive applications or restart the computer, then restart Ollama and
the app. Check Task Manager's **Available** memory before trying again. Model
size shown by `ollama list` is the download size; loading also needs runtime
memory.

```powershell
ollama list
ollama ps
```

If the model still cannot load, pull a smaller model such as `qwen2.5:0.5b`,
set `OLLAMA_MODEL=qwen2.5:0.5b` in `.env`, and restart the app. A smaller model
may produce lower-quality SQL, so always review the generated query.

The workflow does not send a separate model-list request. A missing model is reported from the first Ollama request. Each attempt combines generation and relevance self-check into one response, then the backend validates the SQL locally. This limits a question to at most three Ollama chat calls, including retries. Each call has a hard timeout of at most 120 seconds, so the model portion is bounded to six minutes with the default three attempts. The response is capped at 256 tokens and the model is kept loaded for five minutes. The terminal logs stream progress every 10 seconds. The browser uses one event-stream connection for live tracking.

The relevance check is performed by the same model response that proposes the SQL, not by an independent second model call. SQL syntax and read-only safety are still independently checked by SQLGlot and SQLite before approval or execution. Only one query can run at a time; additional submissions receive HTTP 429 until the current workflow finishes.
