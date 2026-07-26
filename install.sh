#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SLUG="vasp-input-studio"
APP_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || printf '0.0.0')"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$ROOT/.venv"
ENV_FILE="$ROOT/.env.local"
EXAMPLE_ENV="$ROOT/.env.example"
INSTALL_AIIDA="${INSTALL_AIIDA:-0}"

python_version_ok() {
  "$PYTHON_BIN" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Missing Python interpreter: $PYTHON_BIN" >&2
  exit 1
fi

if ! python_version_ok; then
  echo "Python 3.10 or newer is required. Current interpreter: $PYTHON_BIN" >&2
  "$PYTHON_BIN" --version >&2 || true
  exit 1
fi

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  echo "Python venv support is missing for $PYTHON_BIN." >&2
  echo "Install the system venv package first, for example:" >&2
  echo "  Ubuntu/Debian: sudo apt install python3-venv" >&2
  echo "  Fedora/RHEL:   sudo dnf install python3-virtualenv" >&2
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$ROOT/requirements.txt"
if [[ "$INSTALL_AIIDA" == "1" ]]; then
  "$VENV_DIR/bin/pip" install -r "$ROOT/requirements-aiida.txt"
fi

data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
default_workspace="$data_home/$APP_SLUG/workspace"
default_runtime="$state_home/$APP_SLUG/runtime"

mkdir -p "$default_workspace"/systems "$default_workspace"/results "$default_workspace"/scripts "$default_runtime"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<EOF
VASP_STUDIO_WORKSPACE_ROOT=$default_workspace
VASP_STUDIO_RUNTIME_DIR=$default_runtime

# Optional direct-submit integration
# Install note: the installer does not auto-configure the local VASP launcher.
# VASP_STUDIO_VASP_ENV_SCRIPT=/absolute/path/to/vasp-env.sh
# VASP_STUDIO_VASPKIT_CMD=/absolute/path/to/vaspkit
# Install note: without this archive, POTCAR Mapping falls back to plain element names only.
# VASP_STUDIO_POTCAR_ARCHIVE_PBE_64=/absolute/path/to/POTCAR_PBE_64.tar.gz

# Optional AiiDA integration
# Install note: enabling the flag does not create a profile, start the daemon, or register codes/families.
VASP_STUDIO_ENABLE_AIIDA_BACKEND=0
# VASP_STUDIO_AIIDA_PROFILE_NAME=vasp_studio_pg
EOF
elif [[ -f "$EXAMPLE_ENV" ]]; then
  echo "Keeping existing $ENV_FILE" >&2
fi

cat <<EOF
Installed $APP_SLUG $APP_VERSION into:
  $ROOT

Python environment:
  $VENV_DIR

Workspace root:
  $default_workspace

Runtime root:
  $default_runtime

Installer mode:
  Network install (downloads Python dependencies during setup)
  Optional AiiDA extension: $( [[ "$INSTALL_AIIDA" == "1" ]] && printf 'enabled' || printf 'disabled' )

Next steps:
  1. Read $ROOT/INSTALLATION_TROUBLESHOOTING.md before treating the install as production-ready.
  2. Edit $ENV_FILE if you want local VASP submission, POTCAR generation, or AiiDA integration.
  3. Point VASP_STUDIO_VASP_ENV_SCRIPT to a shell file like resources/examples/vasp_env.example.sh.
  4. If you need sv/pv POTCAR variants in the UI, set VASP_STUDIO_POTCAR_ARCHIVE_PBE_64 to a real archive path.
  5. If you need AiiDA, install with INSTALL_AIIDA=1 and then separately enable/configure the profile, daemon, codes, and POTCAR family.
  6. Start the UI with: $ROOT/start.sh
EOF
