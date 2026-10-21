#!/usr/bin/env bash
# Mac: double-click this file. Linux: run  bash export-results.command
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one time; it sets up Python for this app)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "Could not install uv. See TESTING.md."; read -r -p "Press Enter to close"; exit 1; }
fi
uv run --no-dev --python 3.12 python -m scripts.export
read -r -p "Press Enter to close"
