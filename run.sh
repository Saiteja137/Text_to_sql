#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/Scripts/python.exe"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required. Install Node.js 20+ and npm first." >&2
  exit 1
fi
if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  echo "Frontend dependencies are missing. Run: npm --prefix frontend install" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c "import fastapi, langgraph, langchain_ollama, sqlglot, uvicorn" >/dev/null 2>&1; then
  echo "Backend dependencies are missing. Install requirements.txt in the selected Python environment." >&2
  exit 1
fi

backend_pid=""
frontend_pid=""
cleanup() {
  if [[ -n "$backend_pid" ]] && kill -0 "$backend_pid" 2>/dev/null; then
    kill "$backend_pid" 2>/dev/null || true
  fi
  if [[ -n "$frontend_pid" ]] && kill -0 "$frontend_pid" 2>/dev/null; then
    kill "$frontend_pid" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$PYTHON_BIN" -m uvicorn --app-dir "$ROOT_DIR/src" text_to_sql.app:app \
  --host 127.0.0.1 --port 8000 --no-access-log &
backend_pid=$!

npm --prefix "$ROOT_DIR/frontend" run dev -- --host 127.0.0.1 &
frontend_pid=$!

printf 'Backend API: http://127.0.0.1:8000/docs\n'
printf 'Frontend:    http://127.0.0.1:5173\n'
printf 'Press Ctrl+C to stop both services.\n'

while kill -0 "$backend_pid" 2>/dev/null && kill -0 "$frontend_pid" 2>/dev/null; do
  sleep 1
done

echo "A service exited; stopping the other service." >&2
