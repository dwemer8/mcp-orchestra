# Deployment Guide — Local Agents Ensemble

This pipeline is **fully portable**. To stand it up in a fresh container you copy this
folder, run one script with the API key, wire the playbook into context, and (for code
tasks) log in to Codex. Total time: a few minutes.

## TL;DR — what to tell Claude Code

In a fresh container, paste this to Claude Code:

> Bring up the local-agents orchestra per `agents/DEPLOYMENT.md`. The local-API key is `<PASTE_KEY>`.

Claude will run the steps below. Two things only a human can supply:
1. **The `LOCAL_API_KEY`** — it's a secret, never in git. Paste it (or export it before launching).
2. **`codex login`** — interactive ChatGPT auth; run it yourself in a terminal.

Everything else is automated and idempotent (safe to re-run).

## What's in this folder (the entire pipeline)

```
agents/
├── server.py                    # MCP stdio server; auto-loads .env from this folder
├── requirements.txt             # pinned Python deps (mcp, httpx)
├── .env.example                 # documented config template (no secrets) — committed
├── .env                         # YOUR config with the key — created by setup.sh (gitignored)
├── setup.sh                     # one-shot bootstrap: venv + deps + .env + verify + register
├── ORCHESTRATION_PLAYBOOK.md    # routing + cost + fallback rules; loaded as Claude Code context
└── DEPLOYMENT.md                # this file
```

Keep it in version control; porting is just `git clone`. **Never commit `.env`** — only `.env.example`.

---

## Bootstrap steps (what Claude executes)

### 1. Run the bootstrap script with the key

```bash
cd agents
LOCAL_API_KEY='<the key>' bash setup.sh
```

`setup.sh` is path-independent and idempotent. It:
1. ensures `python3` + venv,
2. creates `.venv` and installs pinned deps,
3. creates `.env` from `.env.example` and injects `LOCAL_API_KEY`,
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

### 3. Install + log in to Codex (for code tasks)

The Codex plugin lets Claude hand off final code generation. Marketplace + plugin install
are config (travel via `~/.claude/settings.json`); the login is interactive:

```bash
codex login          # one-time ChatGPT auth, run in a terminal
```

If the plugin isn't installed yet: add the marketplace `openai/codex-plugin-cc` and install
`codex@openai-codex`, then `codex login`.

### 4. Smoke test the whole loop

In a Claude Code session, force a fan-out:

> Use `local_batch` to draft 3 alternative outlines for X on `Qwen/Qwen3.6-35B-A3B`, then show the best one.

If Claude calls the tool, gets parallel results, and reports a shortlist — the pipeline is live.

---

## Portability notes & gotchas

- **The key is the only machine-specific secret.** New container = clone + `LOCAL_API_KEY=... bash setup.sh`.
- **No absolute paths to edit by hand.** `setup.sh` computes them from its own location and
  registers accordingly. Re-running after a move fixes the registration.
- **`.env` is gitignored.** Re-create it per container via `setup.sh` (it copies `.env.example`).
- **`server.py` auto-loads `.env`** from its own directory; exported env vars / `--env` still win.
- **MCP is user scope**, so it works from any working directory in the container — not just `/workspace`.
- **Endpoint is Open WebUI**, hence `/api` (not `/v1`) and explicit `*_PATH` vars. For a raw
  OpenAI-compatible server set `LOCAL_BASE_URL=.../v1`, `LOCAL_MODELS_PATH=/models`,
  `LOCAL_CHAT_COMPLETIONS_PATH=/chat/completions`.
- **If a model (esp. DeepSeek-V4-Pro) is down**, that's expected — the playbook's §4 fallback
  governs behavior. Not a deployment failure.
- **Upgrading later**: `git pull` + re-run `setup.sh` (re-installs deps, re-verifies, re-registers).
