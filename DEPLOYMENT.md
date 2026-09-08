# Deployment Guide — Local Agents Ensemble

This pipeline is **fully portable**. To stand it up in a fresh container you copy this
folder, run one script with the API key, wire the playbook into context, and (for code
tasks) log in to Codex. Total time: a few minutes.

## TL;DR — what to tell Claude Code

In a fresh container, paste this to Claude Code:

> Bring up the local-agents orchestra per `agents/DEPLOYMENT.md`. The local-API key is `<PASTE_KEY>` and the gateway URL is `<PASTE_URL>`.

Claude will run the steps below. Things only a human can supply:
1. **The `LOCAL_API_KEY` and `LOCAL_BASE_URL`** — the key and the private gateway URL. Neither
   is in git (both live only in the gitignored `.env`). Paste them, or export both before launching.
2. **`codex login`** — interactive ChatGPT auth; run it yourself in a terminal.
3. **Approve the Codex sandbox tradeoff** (step 3c) — disabling Codex's sandbox is a
   security decision; Claude will pause and ask before doing it.

Everything else (deps, MCP registration, plugin install, the sandbox patch) is automated
and idempotent (safe to re-run).

## What's in this folder (the entire pipeline)

```
agents/
├── server.py                    # MCP stdio server; auto-loads .env from this folder
├── requirements.txt             # pinned Python deps (mcp, httpx)
├── .env.example                 # documented config template (no secrets) — committed
├── .env                         # YOUR config with the key — created by setup.sh (gitignored)
├── setup.sh                     # one-shot bootstrap: venv + deps + .env + verify + register
├── ORCHESTRATION_PLAYBOOK.md    # routing + cost + fallback rules; loaded as Claude Code context
├── codex-plugin-cc/             # vendored Codex plugin (valid marketplace root; offline install)
└── DEPLOYMENT.md                # this file
```

Keep it in version control; porting is just `git clone`. **Never commit `.env`** — only `.env.example`.

---

## Bootstrap steps (what Claude executes)

### 1. Run the bootstrap script with the key

```bash
cd agents
LOCAL_API_KEY='<the key>' LOCAL_BASE_URL='https://<your-gateway-host>' bash setup.sh
```

`setup.sh` is path-independent and idempotent. It:
1. ensures `python3` + venv,
2. creates `.venv` and installs pinned deps,
3. creates `.env` from `.env.example` and injects `LOCAL_API_KEY` + `LOCAL_BASE_URL` from the
   environment (the URL is private — the committed `.env.example` only carries a placeholder),
4. **verifies connectivity** (`python server.py --list-models` — must print the models, no 401),
5. **registers the MCP server** with Claude Code at **user scope**, path-only:
   `claude mcp add --scope user --transport stdio local-llm -- <abs>/.venv/bin/python <abs>/server.py`

No secret is stored in `~/.claude.json` — `server.py` reads `.env` from its own directory at startup.

Confirm health:

```bash
claude mcp list        # expect: local-llm ... ✔ Connected
```

> If you ran `setup.sh` without the key, it stops before verify/register. Set the key
> (edit `.env` or re-run with `LOCAL_API_KEY=...`) and run it again — it picks up where it left off.

### 2. Wire the playbook into Claude Code context

The routing/cost/fallback discipline must always be active. Ensure a `CLAUDE.md` at your
**working root** imports the playbook. If you work in `/workspace`, create `/workspace/CLAUDE.md`:

```markdown
@agents/ORCHESTRATION_PLAYBOOK.md
```

Claude Code reads `CLAUDE.md` from the cwd and every parent up to `/`, so one file at the
working root covers all subfolders (e.g. `/workspace/causal_discovery`). Use a path relative
to that `CLAUDE.md` (`@agents/...` when it sits beside the `agents/` folder).

### 2b. (Recommended) Register the routing-reminder hook

The playbook loaded via `CLAUDE.md` is base context, but Claude can still drift and
do delegable work itself. `hooks/route-reminder.py` is a `UserPromptSubmit` hook that
inspects each prompt and, when it looks like final code / bulk fan-out / large-material
reading, injects a short targeted reminder to run the ROUTING GATE and delegate to the
right local model + tool. Trivial prompts get nothing.

The script is in the repo (portable via git); the **registration is machine-local** —
add it to your Claude Code `settings.json` (`~/.claude/settings.json`, or the running
user's home) under `hooks`:

```json
"hooks": {
  "UserPromptSubmit": [
    { "hooks": [ { "type": "command",
      "command": "<repo>/.venv/bin/python <repo>/hooks/route-reminder.py" } ] }
  ]
}
```

Use absolute paths (the `.venv/bin/python` created by `setup.sh` is guaranteed present).
Takes effect in **new** Claude Code sessions. Test standalone:
`echo '{"prompt":"напиши функцию"}' | <repo>/.venv/bin/python <repo>/hooks/route-reminder.py`
should print a JSON `additionalContext`; a trivial prompt prints nothing.

### 3. Install Codex + apply the sandbox fix (for code tasks)

Codex is the orchestration's "final code" engine (playbook §1). Three parts: install the
plugin, log in, and — in a locked-down container — disable Codex's own sandbox.

#### 3a. Install the plugin

```bash
claude plugin marketplace add openai/codex-plugin-cc   # or: add ./agents/codex-plugin-cc (offline, pins vendored version)
claude plugin install codex@openai-codex
```

This is the same thing the committed `~/.claude/settings.json` config does
(`extraKnownMarketplaces.openai-codex` + `enabledPlugins."codex@openai-codex"`), so if you
carry over an existing settings.json the plugin travels with it. **Offline / version-pinned
alternative:** this repo vendors the plugin at `agents/codex-plugin-cc` (a valid marketplace
root); `claude plugin marketplace add ./agents/codex-plugin-cc` installs that exact snapshot,
which keeps the companion patch in 3c stable. Restart Claude Code so the plugin loads; verify
with `/codex:setup` or `claude plugin list`.

#### 3b. Log in (human only, interactive)

```bash
codex login          # one-time ChatGPT auth, run in a terminal
```

#### 3c. Sandbox fix — REQUIRED in containers that block user namespaces

Codex sandboxes every command/patch it executes with bundled **bubblewrap**, which must
create a user namespace. Many hardened containers block that (seccomp/capabilities), so Codex
**read-only tasks work but write/exec tasks fail** with:

```
bwrap: No permissions to create a new namespace
```

Detect whether this container is affected:

```bash
unshare --user --map-root-user echo ok    # prints "ok" = unaffected; "Operation not permitted" = affected
```

If affected, the fix is to run Codex **unsandboxed** — the container itself is the isolation
boundary. **Security tradeoff (Claude must ask the human first):** Codex commands then run
directly in-container with no second sandbox and no approval gate; that reach includes
`agents/.env` (the local-API key). Only accept this in a container you trust as the boundary.
There is **no middle ground** — `workspace-write` also uses bwrap and fails the same way; only
`danger-full-access` skips bwrap. Two edits, both setting `danger-full-access`:

1. `~/.codex/config.toml`, at top level:
   ```toml
   sandbox_mode = "danger-full-access"
   ```
2. The plugin **companion hardcodes a per-turn sandbox that overrides config**, so this is the
   edit that actually matters. In
   `~/.claude/plugins/cache/openai-codex/codex/<version>/scripts/codex-companion.mjs`, find the
   write branch (search `request.write ? "workspace-write"`) and change it to
   `request.write ? "danger-full-access" : "read-only"`.

⚠️ **Edit #2 lives in the plugin cache — a codex plugin update/reinstall reverts it.** If write
tasks start failing again with the bwrap error after an update, re-apply edit #2 in the new
version's `codex-companion.mjs`. The durable alternative is host-side: relaunch the container
with namespaces allowed (`--cap-add SYS_ADMIN` / `--security-opt seccomp=unconfined` /
`--privileged`), which keeps Codex's real sandbox and needs neither edit.

Verify write mode after the fix:

> Ask Claude: *Run a Codex write task that creates a throwaway file, then delete it.*

A successful `File changes completed.` (not the bwrap error) means Codex write mode is live.

### 4. Smoke test the whole loop

In a Claude Code session, force a fan-out:

> Use `local_batch` to draft 3 alternative outlines for X on `Qwen/Qwen3.6-35B-A3B`, then show the best one.

If Claude calls the tool, gets parallel results, and reports a shortlist — the pipeline is live.

---

## Portability notes & gotchas

- **The key and the gateway URL are the machine-specific private values.** Neither is committed
  (both live only in the gitignored `.env`). New container = clone + `LOCAL_API_KEY=... LOCAL_BASE_URL=... bash setup.sh`.
- **No absolute paths to edit by hand.** `setup.sh` computes them from its own location and
  registers accordingly. Re-running after a move fixes the registration.
- **`.env` is gitignored.** Re-create it per container via `setup.sh` (it copies `.env.example`).
- **`server.py` auto-loads `.env`** from its own directory; exported env vars / `--env` still win.
- **MCP is user scope**, so it works from any working directory in the container — not just `/workspace`.
- **Endpoint is a LiteLLM-style proxy**: `LOCAL_BASE_URL` is the bare host (no `/api`, no
  `/v1`), with `LOCAL_MODELS_PATH=/models` and `LOCAL_CHAT_COMPLETIONS_PATH=/chat/completions`.
  For a raw `/v1` server set `LOCAL_BASE_URL=.../v1` and keep the same `*_PATH` values.
- **If a model (esp. the heavy code model, GLM-5.3) is down**, that's expected — the
  playbook's §4 fallback governs behavior. Not a deployment failure.
- **Codex write tasks failing with `bwrap: No permissions to create a new namespace`** = the
  sandbox gotcha in step 3c, not a broken install. Read-only Codex still works; apply 3c (or
  fix the container host-side) to enable write mode. Re-check after every codex plugin update.
- **Upgrading later**: `git pull` + re-run `setup.sh` (re-installs deps, re-verifies, re-registers).
