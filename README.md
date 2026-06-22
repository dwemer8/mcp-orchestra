# Local LLM MCP Server

MCP stdio server exposing local OpenAI-compatible chat models to Claude Code.

The server discovers model IDs from `GET $LOCAL_BASE_URL$LOCAL_MODELS_PATH` at startup. It does not hardcode model names. If model discovery fails, the server exits with an actionable error instead of falling back silently.

For `your-gateway-host`, the existing EconCausal harness shows this is an Open WebUI gateway: use `LOCAL_BASE_URL=https://your-gateway-host/api`, `LOCAL_MODELS_PATH=/models`, and `LOCAL_CHAT_COMPLETIONS_PATH=/chat/completions`.

## Install

**Recommended: one command.** From this folder, `setup.sh` builds the venv, installs
deps, writes `.env`, verifies connectivity, and registers the server with Claude Code:

```bash
cd agents
LOCAL_API_KEY='<your key>' bash setup.sh
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
export LOCAL_TIMEOUT_SECONDS=120
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
- `local_generate(model, prompt, system="", temperature=0.7, max_tokens=8192)` runs one completion.
- `local_batch(jobs)` runs many completions concurrently with `LOCAL_MAX_CONCURRENCY` and returns input-ordered per-job results.
- `local_compress(text, model, instruction=..., max_tokens=4096)` summarizes large material through a local model before Claude reads it.

## Runtime Behavior

- `LOCAL_BASE_URL` defaults to `https://your-gateway-host/api`.
- `LOCAL_API_KEY` is sent only when non-empty.
- `LOCAL_MODELS_PATH` defaults to `/models`; `LOCAL_CHAT_COMPLETIONS_PATH` defaults to `/chat/completions`.
- For a raw `/v1` OpenAI-compatible server, set `LOCAL_BASE_URL=https://host`, `LOCAL_MODELS_PATH=/v1/models`, and `LOCAL_CHAT_COMPLETIONS_PATH=/v1/chat/completions`.
- Every HTTP request has a timeout from `LOCAL_TIMEOUT_SECONDS`.
- `429` and `5xx` responses, timeouts, and network errors are retried up to 3 attempts with exponential backoff.
- Structured JSON logs are written to stderr, never stdout.
- Default `max_tokens` is high (8192 for generate/batch, 4096 for compress) on purpose: local reasoning models spend an unpredictable, often large share of the budget on hidden reasoning (observed 3800+ tokens), so a low cap truncates the visible answer. Local models are unlimited, so generous defaults are cheap.
- An empty completion (model exhausted `max_tokens` on reasoning, `finish_reason="length"`) is reported as `ok:false` with diagnostic fields, never as a silent empty success.
