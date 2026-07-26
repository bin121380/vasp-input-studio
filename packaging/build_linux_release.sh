#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT/dist"
ARCH="$(uname -m)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
RELEASE_NAME="vasp-input-studio-${VERSION}-linux-${ARCH}"
STAGE_DIR="$DIST_DIR/$RELEASE_NAME"
ARCHIVE_PATH="$DIST_DIR/${RELEASE_NAME}.tar.gz"

INCLUDE_PATHS=(
  "app"
  "packaging"
  "static"
  "templates"
  "resources"
  "README.md"
  "LICENSE"
  "CITATION.cff"
  "INSTALLATION_TROUBLESHOOTING.md"
  "VERSION"
  "requirements.txt"
  "requirements-aiida.txt"
  "start.sh"
  "install.sh"
  ".env.example"
)

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

for path in "${INCLUDE_PATHS[@]}"; do
  cp -a "$ROOT/$path" "$STAGE_DIR/$path"
done

find "$STAGE_DIR" -type d \( -name "__pycache__" -o -name ".pytest_cache" \) -prune -exec rm -rf {} +
find "$STAGE_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

chmod +x "$STAGE_DIR/start.sh" "$STAGE_DIR/install.sh" "$STAGE_DIR/packaging/build_linux_release.sh" "$STAGE_DIR/resources/scripts/run_step.sh"

mkdir -p "$DIST_DIR"
rm -f "$ARCHIVE_PATH"
tar -C "$DIST_DIR" -czf "$ARCHIVE_PATH" "$RELEASE_NAME"

echo "Created release archive:"
echo "  $ARCHIVE_PATH"
