#!/usr/bin/env bash
# One-command start on macOS/Linux: creates a venv, installs deps, runs the server on http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env, paste your GEMINI_API_KEY into it and run again."; exit 1; }
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
