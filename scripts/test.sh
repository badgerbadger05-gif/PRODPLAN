#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

backend_only=0
frontend_only=0
smoke=0
for arg in "$@"; do
  case "$arg" in
    --backend-only) backend_only=1 ;;
    --frontend-only) frontend_only=1 ;;
    --smoke) smoke=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$frontend_only" -eq 0 ]]; then
  if [[ ! -x ".venv/Scripts/python.exe" && ! -x ".venv/bin/python" ]]; then
    python -m venv .venv
  fi

  if [[ -x ".venv/Scripts/python.exe" ]]; then
    py_cmd=".venv/Scripts/python.exe"
    pytest_cmd=".venv/Scripts/pytest.exe"
  else
    py_cmd=".venv/bin/python"
    pytest_cmd=".venv/bin/pytest"
  fi

  "$py_cmd" -m pip install -r backend/requirements-dev.txt
  "$pytest_cmd" -q
fi

if [[ "$backend_only" -eq 0 ]]; then
  if [[ "$smoke" -eq 1 ]]; then
    (cd frontend-erp-shell && npm run lint && npm run build && npm run smoke)
  else
    (cd frontend-erp-shell && npm run lint && npm run build)
  fi
fi
