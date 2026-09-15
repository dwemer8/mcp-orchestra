#!/usr/bin/env python3
"""UserPromptSubmit hook — reinforces the orchestration ROUTING GATE.

Claude Code runs this on every prompt the user submits. It inspects the prompt
text and, when the request looks like work that should be delegated to the local
models (final code / bulk fan-out / large-blob reading), it injects a short,
targeted routing reminder as additional context — so Claude actually runs the
ROUTING GATE (playbook §0) instead of quietly doing the work on its own tokens.

Trivial prompts (chit-chat, questions, tiny asks) get no injection, to avoid
noise. The full discipline still lives in ORCHESTRATION_PLAYBOOK.md (loaded via
CLAUDE.md); this hook is just the per-message nudge.

Contract (Claude Code hooks):
  stdin  : JSON with at least {"prompt": "<user text>", ...}
  stdout : JSON {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": "..."}}
  exit 0 : always (never block the prompt — a router nudge must not stop work)
"""
import json
import sys

# Model IDs exactly as discovered on the gateway (server.py --list-models).
M_CODE = "zai-org/GLM-5.3"                # final/production code + heavy generation, reasoning=high
M_SECOND = "Qwen/Qwen3.5-397B-A17B-FP8"   # second opinion, and the pre-authorized fallback for M_CODE
M_BULK = "Qwen/Qwen3.6-35B-A3B"           # fan-out, classify, filter, compress

# Keyword triggers. Russian + English, lowercase substring match. Kept broad on
# purpose — a false positive costs a few tokens of reminder; a false negative
# lets a routing leak through, which is the whole thing we're fighting.
CODE_KW = (
    "напиши", "написать", "напишеш", "реализу", "имплемент", "код",
    "функци", "класс", "метод", "рефактор", "исправь", "почини", "багу",
    " баг", "тест", "скрипт", "модул", "патч", "эндпоинт",
    "implement", "write ", "code", "function", "class", "refactor",
    "fix ", "bug", "unit test", "script", "endpoint", "patch",
)
BULK_KW = (
    "сгенериру", "вариант", "классифиц", "отфильтр", "фильтр", "список",
    "перебери", "по каждому", "для каждого", "множеств", "набор ",
    "generate ", "variants", "classify", "filter", "candidates",
    "for each", "batch", "many ",
)
BIG_KW = (
    "лог", "логи", "транскрипт", "дамп", "выгрузк", "простыня",
    "большой файл", "проанализируй файл", "summar", "logs", "transcript",
    "dump", "large file", "analyze the file", "паст",
)


def build_context(prompt: str) -> str:
    low = prompt.lower()
    cats = []
    if any(k in low for k in CODE_KW):
        cats.append("code")
    if any(k in low for k in BULK_KW):
        cats.append("bulk")
    if any(k in low for k in BIG_KW):
        cats.append("large-material")
    if not cats:
        return ""

    lines = [
        "[Routing gate — orchestration playbook §0]",
        "This request looks like: " + ", ".join(cats) + ".",
        "Before doing it yourself, state the one-line Route, then delegate via the",
        "local-llm MCP tools. Applicable routes for this request:",
    ]
    if "code" in cats:
        lines.append(
            f'  - final/production code from a settled spec -> '
            f'local_generate(model="{M_CODE}") — it reasons at "high" by default, no '
            f'parameter needed. Yours: spec + review, not typing it out. '
            f'If it is down, auto-fallback to "{M_SECOND}" is pre-authorized — say which one ran.'
        )
    if "bulk" in cats:
        lines.append(
            f'  - many similar items (candidates/classify/filter/drafts) -> '
            f'local_batch on "{M_BULK}". You review the shortlist, not the raw pile.'
        )
    if "large-material" in cats:
        lines.append(
            f'  - large blob to read (logs/transcripts/dumps) -> '
            f'local_compress on "{M_BULK}" first, then read the concentrate.'
        )
    lines.append(
        "Yours, never delegated: decomposition, judgment, final synthesis, and "
        "correctness review of whatever a model produced. If a local model is "
        "unavailable, follow the §4 fallback discipline (announce it, don't swap silently)."
    )
    return "\n".join(lines)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # malformed input -> stay silent, never block the prompt
    prompt = data.get("prompt") or ""
    context = build_context(prompt)
    if not context:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
