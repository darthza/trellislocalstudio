#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="$(command -v python3 || true)"
if [[ -n "$PYTHON" && ! -x "$PYTHON" ]]; then PYTHON=""; fi
for candidate in /opt/homebrew/bin/python3 /usr/bin/python3 /usr/local/bin/python3; do
  if [[ -z "$PYTHON" && -x "$candidate" ]]; then
    PYTHON="$candidate"
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  cat <<'EOF'

  TRELLIS LOCAL STUDIO · SETUP
  ────────────────────────────────────────────────────────
  Python is needed to start the guided installer.

  Install Homebrew from https://brew.sh, then open Terminal
  in this folder and run:

    brew install python@3.11 uv git
    ./install-asset-studio.command

  ────────────────────────────────────────────────────────
EOF
  read -r "?Press Enter to close this window. "
  exit 1
fi

exec "$PYTHON" tools/install_asset_studio.py
