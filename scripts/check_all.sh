#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

find_python() {
  local preferred="$1"
  if [[ -x "$preferred" ]]; then
    printf '%s\n' "$preferred"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

find_node() {
  if [[ -n "${NODE:-}" && -x "${NODE:-}" ]]; then
    printf '%s\n' "$NODE"
    return 0
  fi
  if [[ -x "$ROOT/.tools/node/bin/node" ]]; then
    printf '%s\n' "$ROOT/.tools/node/bin/node"
    return 0
  fi
  if command -v node >/dev/null 2>&1; then
    command -v node
    return 0
  fi
  return 1
}

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

APP_PYTHON="$(find_python "$ROOT/.venv/bin/python")" || {
  echo "No Python interpreter found for app checks." >&2
  exit 1
}
TEST_PYTHON="$APP_PYTHON"
if [[ -x "$ROOT/.venv-test/bin/python" ]]; then
  TEST_PYTHON="$ROOT/.venv-test/bin/python"
fi
NODE_BIN="$(find_node)" || {
  echo "No Node.js runtime found. Install Node or set NODE=/path/to/node to check static/app.js." >&2
  exit 1
}

run "$APP_PYTHON" -m compileall app
run "$NODE_BIN" --check static/app.js
run "$TEST_PYTHON" -m unittest discover -s tests -v

printf '\nAll checks passed.\n'
