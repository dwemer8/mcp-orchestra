"""Tests for routing reminders in the active Codex mode."""

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "route_reminder", REPO_ROOT / "hooks" / "route-reminder.py"
)
route_reminder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route_reminder)


def test_codex_enabled_on_symlink(tmp_path):
    """Enable Codex for an on-target symlink even when its target is missing."""
    link = tmp_path / "ORCHESTRATION_PLAYBOOK.md"
    link.symlink_to("playbooks/codex-on.md")
    assert route_reminder.codex_enabled(link) is True


def test_codex_enabled_off_symlink(tmp_path):
    """Disable Codex for an off-target symlink."""
    link = tmp_path / "ORCHESTRATION_PLAYBOOK.md"
    link.symlink_to("playbooks/codex-off.md")
    assert route_reminder.codex_enabled(link) is False


def test_codex_enabled_regular_file(tmp_path):
    """Disable Codex for a regular file."""
    path = tmp_path / "codex-on.md"
    path.write_text("playbooks/codex-on.md")
    assert route_reminder.codex_enabled(path) is False


def test_codex_enabled_missing_path(tmp_path):
    """Disable Codex when the playbook path is missing."""
    assert route_reminder.codex_enabled(tmp_path / "ORCHESTRATION_PLAYBOOK.md") is False


def test_build_context_codex_on():
    """Route final code through Codex with an explicit GLM fallback."""
    context = route_reminder.build_context("напиши функцию", codex_on=True)
    assert "Codex via the codex plugin" in context
    assert "delegate via Codex" in context
    assert (
        'If Codex is unavailable, fall back to local_generate(model="zai-org/GLM-5.3") '
        '(reasoning "high" by default) and say which one ran.'
    ) in context


def test_build_context_codex_off():
    """Keep the local model route when Codex is disabled."""
    context = route_reminder.build_context("напиши функцию", codex_on=False)
    assert 'local_generate(model="zai-org/GLM-5.3")' in context
    assert "codex plugin" not in context


def test_build_context_trivial_prompt():
    """Keep trivial prompts silent even in Codex mode."""
    assert route_reminder.build_context("привет", codex_on=True) == ""


def test_build_context_bulk_only():
    """Keep bulk-only routing identical across modes."""
    prompt = "отфильтруй эти варианты"
    context = route_reminder.build_context(prompt, codex_on=True)
    assert "bulk" in context and "code" not in context.split("\n")[1]
    assert context == route_reminder.build_context(prompt, codex_on=False)
