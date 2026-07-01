#!/usr/bin/env bash
# setup.sh — one-shot bootstrap for the local-llm MCP server.
# Idempotent and path-independent: clone/copy this folder anywhere and run it.
#
# Brings up: venv + deps + .env + connectivity check + Claude Code registration.
# The ONE thing it can't invent is the secret. Provide it either way:
#   LOCAL_API_KEY=sk-... bash setup.sh      # non-interactive (recommended for agents)
#   bash setup.sh                            # then edit .env and re-run to verify+register
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> [1/6] Checking Python 3"
if ! command -v python3 >/dev/null 2>&1; then
  echo "    python3 not found. Installing..."
  apt-get update -y && apt-get install -y python3 python3-venv python3-pip
fi
echo "    python3 = $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')"

echo "==> [2/6] Creating virtualenv (.venv)"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> [3/6] Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "    deps installed"

echo "==> [4/6] Preparing .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    Created .env from .env.example"
fi
# Inject LOCAL_API_KEY from the environment if provided and not already set in .env.
if [ -n "${LOCAL_API_KEY:-}" ]; then
  if grep -qE '^LOCAL_API_KEY=.+' .env; then
    echo "    .env already has a LOCAL_API_KEY; leaving it untouched"
  else
    # replace the empty assignment in place
    tmp="$(mktemp)"
    sed "s|^LOCAL_API_KEY=.*|LOCAL_API_KEY=${LOCAL_API_KEY}|" .env > "$tmp" && mv "$tmp" .env
    echo "    Wrote LOCAL_API_KEY from environment into .env"
  fi
fi
# The endpoint URL is private (never committed). Inject it from the environment if
# provided; otherwise .env still holds the placeholder from .env.example and must be
# edited by hand before verification will pass.
if [ -n "${LOCAL_BASE_URL:-}" ]; then
  tmp="$(mktemp)"
  sed "s|^LOCAL_BASE_URL=.*|LOCAL_BASE_URL=${LOCAL_BASE_URL}|" .env > "$tmp" && mv "$tmp" .env
  echo "    Wrote LOCAL_BASE_URL from environment into .env"
fi

echo "==> [5/6] Verifying connectivity (server.py reads .env itself)"
if grep -qE '^LOCAL_API_KEY=.+' .env; then
  set +e
  python server.py --list-models >/tmp/local-llm-models.json 2>/tmp/local-llm-discovery.log
  RC=$?
  set -e
  if [ $RC -ne 0 ]; then
    echo "    Model discovery FAILED. Details:"
    sed 's/^/      /' /tmp/local-llm-discovery.log
    echo "    Fix LOCAL_API_KEY / LOCAL_BASE_URL in .env and re-run."
    exit 1
  fi
  echo "    OK — discovered models:"
  python -c "import json;print('      '+', '.join(json.load(open('/tmp/local-llm-models.json'))['models']))"
else
  echo "    LOCAL_API_KEY not set in .env. Set it (edit .env or re-run with LOCAL_API_KEY=...)."
  echo "    Skipping verification + registration until the key is present."
  exit 0
fi

echo "==> [6/6] Registering MCP server with Claude Code (user scope, path-only)"
if ! command -v claude >/dev/null 2>&1; then
  echo "    'claude' CLI not found on PATH — skipping registration."
  echo "    Once Claude Code is installed, register manually with:"
  echo "      claude mcp add --scope user --transport stdio local-llm -- $HERE/.venv/bin/python $HERE/server.py"
  exit 0
fi
# Re-register cleanly so paths are always correct for THIS folder. No secret in the
# command: server.py loads .env from its own directory.
claude mcp remove local-llm >/dev/null 2>&1 || true
claude mcp add --scope user --transport stdio local-llm \
  -- "$HERE/.venv/bin/python" "$HERE/server.py"
echo ""
echo "==> Done. Verify health:  claude mcp list   (expect: local-llm ... ✔ Connected)"
echo "    Playbook is loaded via CLAUDE.md. Codex needs a one-time: codex login"
