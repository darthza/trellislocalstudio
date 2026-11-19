#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"
exec /opt/homebrew/bin/python3 tools/run_asset_studio.py mcp "$@"
