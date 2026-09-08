#!/usr/bin/env bash
# codex-toggle.sh — switch the orchestration playbook between Codex-enabled and
# Codex-disabled routing, and enable/disable the Codex plugin in Claude Code to match.
#
#   ./codex-toggle.sh on      # route final code to Codex (plugin must be installed)
#   ./codex-toggle.sh off     # route final code to GLM-5.3 (no Codex)
#   ./codex-toggle.sh status  # show current state
#
# The change takes effect in NEW Claude Code sessions (context is read at startup).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK="$HERE/ORCHESTRATION_PLAYBOOK.md"
ON_TARGET="playbooks/codex-on.md"
OFF_TARGET="playbooks/codex-off.md"
PLUGIN="codex@openai-codex"

plugin_installed() {
  command -v claude >/dev/null 2>&1 && claude plugin list 2>/dev/null | grep -q "$PLUGIN"
}

current_target() {
  if [ -L "$LINK" ]; then
    readlink "$LINK"
  else
    echo "(not a symlink — run 'on' or 'off' to fix)"
  fi
}

status() {
  echo "Playbook: $(current_target)"
  if ! command -v claude >/dev/null 2>&1; then
    echo "Plugin:   'claude' CLI not found — cannot query plugin state"
  elif plugin_installed; then
    echo "Plugin:   $PLUGIN installed ($(claude plugin list 2>/dev/null | grep "$PLUGIN" || true))"
  else
    echo "Plugin:   $PLUGIN not installed"
  fi
}

case "${1:-status}" in
  on)
    ln -sfn "$ON_TARGET" "$LINK"
    echo "Playbook -> $ON_TARGET (final code routes to Codex)"
    if plugin_installed; then
      claude plugin enable "$PLUGIN" >/dev/null 2>&1 || true
      echo "Plugin   -> $PLUGIN enabled"
    else
      echo "WARNING: the Codex plugin is NOT installed, but the playbook now expects it."
      echo "Install it first:"
      echo "  claude plugin marketplace add openai/codex-plugin-cc   # or the vendored ./codex-plugin-cc"
      echo "  claude plugin install $PLUGIN"
      echo "  codex login    # one-time interactive ChatGPT auth"
    fi
    echo "Takes effect in new Claude Code sessions."
    ;;
  off)
    ln -sfn "$OFF_TARGET" "$LINK"
    echo "Playbook -> $OFF_TARGET (final code routes to GLM-5.3, Claude reviews)"
    if plugin_installed; then
      claude plugin disable "$PLUGIN" >/dev/null 2>&1 || true
      echo "Plugin   -> $PLUGIN disabled"
    fi
    echo "Takes effect in new Claude Code sessions."
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $0 on|off|status" >&2
    exit 2
    ;;
esac
