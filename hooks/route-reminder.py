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
The ORCHESTRATION_PLAYBOOK.md symlink target determines whether Codex mode is active.

Contract (Claude Code hooks):
  stdin  : JSON with at least {"prompt": "<user text>", ...}
  stdout : JSON {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": "..."}}
  exit 0 : always (never block the prompt — a router nudge must not stop work)
"""
import json
import os
import sys
from pathlib import Path

# Model IDs exactly as discovered on the gateway (server.py --list-models).
M_CODE = "zai-org/GLM-5.3"                # final/production code + heavy generation, reasoning=high
M_SECOND = "Qwen/Qwen3.5-397B-A17B-FP8"   # second opinion, and the pre-authorized fallback for M_CODE
M_BULK = "Qwen/Qwen3.6-35B-A3B"           # fan-out, classify, filter, compress

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_LINK = REPO_ROOT / "ORCHESTRATION_PLAYBOOK.md"
CODEX_ON_TARGET = "codex-on.md"

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
# Report text goes to Codex only in Codex mode.
REPORT_KW = (
    "отчёт", "отчет", "описание pr", "описание mr", "описание пр", "описание мр",
    "youtrack", "ютрек", "статус-апдейт", "write-up", "writeup", "report",
    "pr description", "mr description", "status update",
)


def codex_enabled(playbook_link: Path) -> bool:
    """Detect Codex mode from the playbook symlink target.

    Args:
        playbook_link: Path to the active orchestration playbook symlink.

    Returns:
        True if the symlink target basename is codex-on.md; False on OSError.
    """
    try:
        return (
            playbook_link.is_symlink()
            and Path(os.readlink(playbook_link)).name == CODEX_ON_TARGET
        )
    except OSError:
        return False


def build_context(prompt: str, codex_on: bool = False) -> str:
    """Build a routing reminder for the prompt and active mode.

    Args:
        prompt: User text to classify by routing keywords.
        codex_on: Whether final code and report text should be routed to Codex.

    Returns:
        Applicable routing context, or an empty string if no keywords match.
    """
    low = prompt.lower()
    cats = []
    if any(k in low for k in CODE_KW):
        cats.append("code")
    if any(k in low for k in BULK_KW):
        cats.append("bulk")
    if any(k in low for k in BIG_KW):
        cats.append("large-material")
    if codex_on and any(k in low for k in REPORT_KW):
        cats.append("report")
    if not cats:
        return ""

    lines = [
        "[Routing gate — orchestration playbook §0]",
        "This request looks like: " + ", ".join(cats) + ".",
        ("Before doing it yourself, state the one-line Route, then delegate via Codex or the"
         if codex_on and ("code" in cats or "report" in cats) else
         "Before doing it yourself, state the one-line Route, then delegate via the"),
        "local-llm MCP tools. Applicable routes for this request:",
    ]
    if "code" in cats:
        if codex_on:
            lines.append(
                f'  - final/production code from a settled spec -> '
                f'Codex via the codex plugin (/codex:rescue, or codex-companion.mjs task --write). '
                f'Yours: spec + review, not typing it out. If Codex is unavailable, fall back to '
                f'local_generate(model="{M_CODE}") (reasoning "high" by default) and say which one ran.'
            )
        else:
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
    if "report" in cats:
        lines.append(
            '  - report text (tracker comment, PR/MR description, write-up) -> '
            'Codex via the codex plugin. Yours: the brief (facts, measured numbers with sources, '
            'layout, audience) and the review of every number and claim. '
            'If Codex is unavailable, write the report yourself — no fallback to GLM-5.3 '
            'or other local models.'
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
    context = build_context(prompt, codex_enabled(PLAYBOOK_LINK))
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
