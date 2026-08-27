# Local LLM MCP Server

MCP stdio server exposing local OpenAI-compatible chat models to Claude Code.

The server discovers model IDs from `GET $LOCAL_BASE_URL$LOCAL_MODELS_PATH` at startup. It does not hardcode model names. If model discovery fails, the server exits with an actionable error instead of falling back silently.

For an Open WebUI gateway, set `LOCAL_BASE_URL=https://<your-gateway-host>/api`, `LOCAL_MODELS_PATH=/models`, and `LOCAL_CHAT_COMPLETIONS_PATH=/chat/completions`. The endpoint URL is private — keep it in `.env` (gitignored), never in committed files.

## Install

**Recommended: one command.** From this folder, `setup.sh` builds the venv, installs
deps, writes `.env`, verifies connectivity, and registers the server with Claude Code:

```bash
cd agents
LOCAL_API_KEY='<your key>' LOCAL_BASE_URL='https://<your-gateway-host>/api' bash setup.sh
```

See `DEPLOYMENT.md` for the full bootstrap (including the playbook and Codex). Manual
install if you prefer:

```bash
cd agents
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Configure

Environment variables:

```bash
export LOCAL_BASE_URL=https://your-gateway-host/api
export LOCAL_MODELS_PATH=/models
export LOCAL_CHAT_COMPLETIONS_PATH=/chat/completions
export LOCAL_API_KEY=
export LOCAL_MAX_CONCURRENCY=8
export LOCAL_TIMEOUT_SECONDS=1800
export LOCAL_REASONING_FIELDS=effort+thinking
```

`LOCAL_API_KEY` is optional. When non-empty, requests include:

```text
Authorization: Bearer <LOCAL_API_KEY>
```

See `.env.example` for all variables.

## Verify Connectivity

Run model discovery before registering the server (`server.py` auto-loads `.env` from
its own folder, so no exports needed):

```bash
cd agents
.venv/bin/python server.py --list-models
```

The command prints discovered model IDs and metadata as JSON to stdout. Logs go to stderr so they do not interfere with MCP stdio when the server is running normally. A loud 401 means `LOCAL_API_KEY` is missing or wrong.

## Register With Claude Code

`setup.sh` does this for you. To register manually, use **user scope** and **absolute
paths** to this folder's venv and server. No `--env` flags are needed — `server.py` reads
`.env` itself, which keeps the secret out of `~/.claude.json`:

```bash
claude mcp add --scope user --transport stdio local-llm \
  -- "$PWD/.venv/bin/python" "$PWD/server.py"
```

(Run it from inside the `agents/` folder so `$PWD` resolves correctly.)

Check status inside Claude Code with:

```text
/mcp
```

## Tools

- `list_local_models()` returns discovered model IDs and metadata.
- `local_generate(model, prompt, system="", temperature=0.7, max_tokens=None, reasoning="")` runs one completion.
- `local_batch(jobs)` runs many completions concurrently with `LOCAL_MAX_CONCURRENCY` and returns input-ordered per-job results. Each job: `{model, prompt, system?, temperature?, max_tokens?, reasoning?}`.
- `local_compress(text, model, instruction=..., max_tokens=4096)` summarizes large material through a local model before Claude reads it. Reasoning is forced `off` — compression is mechanical and a chain of thought would eat the budget meant for the concentrate.

### Reasoning

`reasoning` accepts `off`, `low`, `medium`, `high`, `max`. Leave it empty to take the
model's default depth:

| Model | Default |
|---|---|
| `deepseek-ai/DeepSeek-V4-Pro` | `high` |
| everything else | `off` |

This is why the code route needs no extra argument. Mechanics, measured against the
live gateway:

- Two request fields are involved, and they are **not** symmetric.
  `chat_template_kwargs.thinking` is the actual switch; `reasoning_effort` only grades
  depth while thinking is on — sent alone it is silently ignored. `LOCAL_REASONING_FIELDS`
  (`effort+thinking` | `thinking` | `off`) exists as an escape hatch if an endpoint
  rejects one of them.
- Reasoning is billed inside `completion_tokens`, so the answer and the chain of thought
  share one budget. Leave `max_tokens` unset and the server picks one that fits the depth:
  32768 when reasoning is on, 8192 otherwise.
- The chain of thought is **not** returned — reading it would burn the orchestrator budget
  this server exists to protect. `reasoning_chars` reports its size instead.
- `reasoning` in a result is *what was requested*. Some models (Qwen3.6-35B among them)
  reason on their own regardless, so `reasoning: "off"` alongside a non-zero
  `reasoning_chars` is expected, not a contradiction.
- `usage.reasoning_tokens` is unreliable and model-dependent: DeepSeek-V4-Pro reports `0`
  even after a 1000+ character chain of thought, while Qwen3.6-35B fills it in. Trust
  `reasoning_chars`, which is measured from the response itself.
- If the endpoint rejects the reasoning fields (HTTP 400/422), the call is retried once
  without them and the result carries `reasoning_downgraded: true` rather than failing.
- If a model returns its answer in `reasoning_content` and leaves `content` empty, that
  text is used as the answer and flagged with `content_from_reasoning: true`.

## Runtime Behavior

- `LOCAL_BASE_URL` has no baked-in default — set it in `.env` (the endpoint is private and never committed).
- `LOCAL_API_KEY` is sent only when non-empty.
- `LOCAL_MODELS_PATH` defaults to `/models`; `LOCAL_CHAT_COMPLETIONS_PATH` defaults to `/chat/completions`.
- For a raw `/v1` OpenAI-compatible server, set `LOCAL_BASE_URL=https://host`, `LOCAL_MODELS_PATH=/v1/models`, and `LOCAL_CHAT_COMPLETIONS_PATH=/v1/chat/completions`.
- Every HTTP request has a timeout from `LOCAL_TIMEOUT_SECONDS` (default 1800s). Deep reasoning on a code task runs for minutes; the previous 120s cut it off mid-thought, which looked like an unreachable model and tripped the playbook's fallback rules for no reason.
- `429` and `5xx` responses, timeouts, and network errors are retried up to 3 attempts with exponential backoff.
- Structured JSON logs are written to stderr, never stdout.
- Default `max_tokens` is high (8192 for generate/batch, 4096 for compress) on purpose: local reasoning models spend an unpredictable, often large share of the budget on hidden reasoning (observed 3800+ tokens), so a low cap truncates the visible answer. Local models are unlimited, so generous defaults are cheap.
- An empty completion (model exhausted `max_tokens` on reasoning, `finish_reason="length"`) is reported as `ok:false` with diagnostic fields, never as a silent empty success.

## Codex on/off (optional)

Codex is **optional**. Out of the box this repo runs Codex-disabled: final code from a
settled spec is routed to **DeepSeek-V4-Pro** (reasoning `high`) via `local_generate`, and
Claude reviews the result. Everything works with the local models alone — no Codex install,
login, or subscription needed.

The routing playbook has two variants under `playbooks/` and `ORCHESTRATION_PLAYBOOK.md`
is a symlink to the active one. Flip between them with:

```bash
./codex-toggle.sh off      # default: final code -> DeepSeek-V4-Pro (reasoning high), Claude reviews
./codex-toggle.sh on       # final code -> Codex (installs/enable steps printed if missing)
./codex-toggle.sh status   # show the active playbook + plugin state
```

`on` also enables the `codex@openai-codex` plugin (and prints install instructions if it
isn't there); `off` disables it. Changes take effect in **new** Claude Code sessions,
since the playbook is read as context at startup. See `DEPLOYMENT.md` step 3 for the full
Codex install + login flow.

## Codex Sandbox Gotcha (write tasks fail with `bwrap`)

Codex is the orchestration's "final code" engine, reached through the `codex@openai-codex`
plugin. In a hardened container you will hit this:

- **Symptom:** Codex **read-only** tasks (reviews, analysis) work, but **write/exec** tasks
  fail with `bwrap: No permissions to create a new namespace`.
- **Cause:** Codex wraps every executed command/patch in bundled **bubblewrap**, which must
  create a user namespace. The container blocks namespace creation at the seccomp/capabilities
  level (`unshare --user` → `Operation not permitted`) — even when the
  `kernel.unprivileged_userns_clone` sysctl is set. It's a container-runtime lock, not
  something you can toggle from inside.
- **No middle ground:** `workspace-write` also uses bwrap and fails identically; only
  `danger-full-access` skips the sandbox entirely.
- **Fix (security tradeoff — the container becomes the only isolation boundary):** set
  `sandbox_mode = "danger-full-access"` in `~/.codex/config.toml` **and** change the plugin
  companion's per-turn sandbox (which overrides config) — in
  `~/.claude/plugins/cache/openai-codex/codex/<version>/scripts/codex-companion.mjs`, the write
  branch `request.write ? "workspace-write"` → `"danger-full-access"`. Codex commands then run
  unsandboxed in-container (no approval gate; `agents/.env` is reachable) — only do this in a
  container you trust as the boundary.
- **Fragile:** the companion edit lives in the plugin cache and is **reverted by a codex plugin
  update** — re-apply it in the new version dir if the bwrap error returns. Durable alternative:
  relaunch the container host-side with namespaces allowed (`--cap-add SYS_ADMIN` /
  `--security-opt seccomp=unconfined` / `--privileged`), which keeps Codex's real sandbox.

Full step-by-step (plugin install + login + this fix) is in `DEPLOYMENT.md` step 3.
