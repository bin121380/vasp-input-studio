#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${VASP_STUDIO_HOST:-127.0.0.1}"
PORT="${VASP_STUDIO_PORT:-8010}"

ARGS=(app.main:app --app-dir "$ROOT" --host "$HOST" --port "$PORT")
if [[ "${VASP_STUDIO_RELOAD:-0}" == "1" ]]; then
  ARGS+=(--reload)
fi

choose_python() {
  if [[ -n "${VASP_STUDIO_PYTHON:-}" && -x "${VASP_STUDIO_PYTHON}" ]]; then
    printf '%s\n' "${VASP_STUDIO_PYTHON}"
    return 0
  fi
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3)"
    return 0
  fi
  return 1
}

PYTHON_BIN="$(choose_python || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "No suitable Python interpreter was found." >&2
  exit 1
fi

if ! "${PYTHON_BIN}" -c "import uvicorn" >/dev/null 2>&1; then
  echo "Uvicorn is not installed for ${PYTHON_BIN}." >&2
  echo "Run these commands first:" >&2
  echo "  cd $ROOT" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ "${PYTHON_BIN}" == "$ROOT/.venv/bin/python" ]]; then
  export PATH="$ROOT/.venv/bin:$PATH"
fi

exec "${PYTHON_BIN}" -m uvicorn "${ARGS[@]}"
