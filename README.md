# College Text-to-SQL

A full-stack natural-language-to-SQL demo with a **FastAPI** backend, **React +
TypeScript** frontend, **LangGraph** orchestration, the **OpenAI Responses API**,
and a sample **SQLite** college database.

The workflow reads the current schema, asks OpenAI for one SQLite `SELECT`,
checks the result with SQLGlot and SQLite's read-only authorizer, and retries
validation or relevance failures at most three times. A tracking agent records
each stage so the UI can show live progress. Human approval by email is optional.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.10+
- Node.js 20+ and npm
- An OpenAI API project with billing enabled and an API key
- An SMTP/IMAP-enabled mailbox when email approval is enabled

## 1. Install the backend

From the project directory in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
python -m text_to_sql.init_db
```

If PowerShell prevents activation, run commands with
`.\.venv\Scripts\python.exe` directly.

## 2. Configure OpenAI

Create the local environment file:

```powershell
Copy-Item .env.example .env
```

Create an API key in your OpenAI project and put it only in the local `.env`:

```dotenv
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-6-luna
OPENAI_TIMEOUT_SECONDS=120
```

`gpt-6-luna` is the low-cost default. Change `OPENAI_MODEL` to another model
available to your project if needed. API usage is billed separately from a
ChatGPT subscription. `.env` is ignored by Git; never commit or paste a real key.

The app uses the OpenAI Responses API with a strict JSON schema. One response
contains both the proposed SQL and its relevance self-check. The request uses
`store=false`, so it is not stored for later retrieval through the API.

## 3. Configure execution and optional approval

The default `APPROVAL_REQUIRED=false` executes validated SQL immediately through
a read-only SQLite connection. To require a human decision first, set:

```dotenv
APPROVAL_REQUIRED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-sender@gmail.com
SMTP_PASSWORD=your-google-app-password
SMTP_USE_TLS=true
MAIL_FROM=your-sender@gmail.com
APPROVER_EMAIL=approver@example.com
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=your-sender@gmail.com
IMAP_PASSWORD=your-google-app-password
IMAP_POLL_INTERVAL_SECONDS=15
```

For Gmail, use an app password on an account with 2-Step Verification. SMTP sends
the request; IMAP receives replies. The application accepts only a reply from
`APPROVER_EMAIL` with the request ID in the subject. A reply must begin with
`APPROVE` or `REJECT`; following text becomes a comment.

`APPROVAL_BASE_URL` points to the React approval page. The local default works
only when the email recipient can reach this computer. For remote approval,
deploy both services behind HTTPS, use a reachable frontend URL, and update
`CORS_ORIGINS`. Approval links are bearer secrets.

## 4. Start the application

Install frontend packages once:

```powershell
npm.cmd --prefix frontend install
```

On Windows PowerShell:

```powershell
.\run.ps1
```

On Linux/macOS, WSL, or Git Bash:

```bash
chmod +x run.sh
./run.sh
```

The launchers start FastAPI and Vite together. No local model server is needed.
Open <http://localhost:5173>. API health and interactive docs are at
<http://127.0.0.1:8000/api/health> and <http://127.0.0.1:8000/docs>.

Example question:

> What is the average salary of employees in the Artificial Intelligence department?

## Workflow and safety

1. Read SQLite tables, columns, and foreign-key relationships. Database rows are
   not included in the OpenAI request.
2. Send the schema and question to OpenAI. Structured output returns SQL and a
   relevance assessment in one response.
3. Validate the statement with SQLGlot and prepare `EXPLAIN QUERY PLAN` on a
   `mode=ro`, `query_only` SQLite connection with an authorizer.
4. Retry failed validation or relevance checks, with at most three OpenAI API
   requests per question.
5. Execute immediately in direct mode, or email the validated query and wait for
   explicit approval in approval mode.
6. Revalidate before execution, enforce a query timeout, and cap result rows.

These safeguards prevent database writes but do not guarantee that generated SQL
has the intended meaning. Review important queries carefully.

For the detailed workflow, see [docs/flowdiagram.md](docs/flowdiagram.md). For the
user-facing frontend path, see [docs/frontend-flow.md](docs/frontend-flow.md).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | required | OpenAI project API key |
| `OPENAI_MODEL` | `gpt-6-luna` | Model for SQL generation and relevance checking |
| `OPENAI_TIMEOUT_SECONDS` | `120` | Per-request timeout in seconds (1-120) |
| `DATABASE_PATH` | `data/college.db` | College SQLite database |
| `APPROVAL_DB_PATH` | `data/approvals.db` | Approval requests and tracking events |
| `MAX_GENERATION_ATTEMPTS` | `3` | Maximum OpenAI requests per question (1-3) |
| `QUERY_TIMEOUT_SECONDS` | `5` | SQLite query timeout |
| `MAX_RESULT_ROWS` | `200` | Maximum rows returned |
| `APPROVAL_REQUIRED` | `false` | Require an email/link decision before execution |
| `APPROVAL_EXPIRY_MINUTES` | `20` | Approval lifetime (1-20 minutes) |
| `APPROVAL_BASE_URL` | local React URL | Approval page URL |
| `CORS_ORIGINS` | local Vite origins | Comma-separated frontend origins |

See `.env.example` for SMTP and IMAP settings.

## Production frontend build

```powershell
cd frontend
npm install
npm run build
cd ..
```

FastAPI serves `frontend/dist` at `/` and `/approval` after the build. Use an
appropriate production ASGI server and HTTPS deployment; do not expose Vite's
development server publicly.

## Tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked OpenAI/SMTP/IMAP clients. They do not
make API calls or send email.

## OpenAI troubleshooting

- `OPENAI_API_KEY is missing`: add the key to `.env`, then restart the backend.
- HTTP 401: create or copy a valid project key and check for extra whitespace.
- HTTP 404: set `OPENAI_MODEL` to a model available to the API project.
- HTTP 429: check project billing, quota, and rate limits.
- Connection or timeout errors: check the network and increase
  `OPENAI_TIMEOUT_SECONDS` up to 120 if appropriate.

The SDK's automatic retries are disabled because LangGraph owns the visible
three-attempt workflow budget.
