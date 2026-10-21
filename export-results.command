#!/usr/bin/env bash
# Mac: double-click this file. Linux: run  bash export-results.command
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$PATH"
# The app's Python environment lives outside this folder, so iCloud/Dropbox don't sync it.
if [ "$(uname)" = "Darwin" ]; then base="$HOME/Library/Application Support"; else base="${XDG_DATA_HOME:-$HOME/.local/share}"; fi
export UV_PROJECT_ENVIRONMENT="$base/dot-voice-agent/venv"
if ! command -v uv >/dev/null 2>&1; then
  echo "First time only: installing the tools this needs..."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null || { echo; echo "Couldn't download the tools. Check your internet connection and try again."; read -r -p "Press Enter to close"; exit 1; }
fi
[ -d "$UV_PROJECT_ENVIRONMENT" ] || echo "First time only: downloading what the agent needs. This takes about 5 minutes..."
uv run --no-dev --python 3.12 python -m scripts.export
echo
read -r -p "Press Enter to close this window"
