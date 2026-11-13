#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  command -v ollama >/dev/null && (ollama serve >/tmp/ollama-serve.log 2>&1 &) && sleep 2
fi
source .venv/bin/activate
exec python src/main.py "$@"
