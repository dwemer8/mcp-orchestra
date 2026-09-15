"""Tests for the reasoning knobs: level resolution, profiles, payload fields, usage."""

import pytest

from server import (
    REASONING_LEVELS,
    reasoning_payload_fields,
    reasoning_profile,
    resolve_reasoning,
    usage_token_count,
)

# The mode production code runs by default; the narrower modes get their own tests.
EFFORT_AND_THINKING = "effort+thinking"
THINKING_ONLY = "thinking"
MODE_OFF = "off"

GLM = "zai-org/GLM-5.3"
QWEN3_SMALL = "Qwen/Qwen3.6-35B-A3B"
QWEN3_LARGE = "Qwen/Qwen3.5-397B-A17B-FP8"


# --- resolve_reasoning -----------------------------------------------------


def test_resolve_reasoning_glm_default_is_high():
    """An empty request on GLM falls back to the per-model default "high"."""
    assert resolve_reasoning(GLM, "") == "high"


def test_resolve_reasoning_rejects_max_on_glm():
    """An explicit "max" on GLM is an error that points the caller at "high"."""
    with pytest.raises(ValueError, match='not allowed for model .*use "high"'):
        resolve_reasoning(GLM, "max")


@pytest.mark.parametrize("model", [QWEN3_SMALL, QWEN3_LARGE, "openai/gpt-oss-120b"])
def test_resolve_reasoning_max_allowed_elsewhere(model):
    """Only GLM rejects "max"; every other model still accepts it."""
    assert resolve_reasoning(model, "max") == "max"


def test_resolve_reasoning_unknown_model_defaults_to_off():
    """An empty request on a model without a default resolves to "off"."""
    assert resolve_reasoning("some/unknown-model", "") == "off"


def test_resolve_reasoning_explicit_level_beats_default():
    """An explicit level wins over the per-model default."""
    assert resolve_reasoning(GLM, "low") == "low"


def test_resolve_reasoning_normalises_case_and_whitespace():
    """Requests are stripped and lowercased before validation."""
    assert resolve_reasoning(GLM, "  High ") == "high"


def test_resolve_reasoning_rejects_unknown_level():
    """A level outside REASONING_LEVELS raises ValueError naming the valid ones."""
    with pytest.raises(ValueError, match="Valid levels"):
        resolve_reasoning(GLM, "extreme")


# --- reasoning_profile -----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected_profile"),
    [
        (GLM, "glm"),
        (QWEN3_SMALL, "qwen3"),
        (QWEN3_LARGE, "qwen3"),
    ],
)
def test_reasoning_profile_known_models(model, expected_profile):
    """Each measured model maps to the profile naming its field set."""
    assert reasoning_profile(model) == expected_profile


def test_reasoning_profile_unknown_model_is_default():
    """Models without a measured field set use the default profile."""
    assert reasoning_profile("openai/gpt-oss-120b") == "default"


# --- reasoning_payload_fields ----------------------------------------------


@pytest.mark.parametrize(
    ("profile", "level"),
    [
        ("glm", "off"),
        ("glm", "max"),
        ("qwen3", "off"),
        ("qwen3", "max"),
        ("default", "off"),
        ("default", "max"),
    ],
)
def test_mode_off_strips_all_fields(profile, level):
    """Mode "off" is the escape hatch: no fields for any profile or level."""
    assert reasoning_payload_fields(level, MODE_OFF, profile) == {}


def test_glm_off_sends_template_low_without_top_level_effort():
    """GLM "off" maps to template "low" and adds no top-level effort."""
    fields = reasoning_payload_fields("off", EFFORT_AND_THINKING, "glm")
    assert fields == {"chat_template_kwargs": {"reasoning_effort": "low"}}


@pytest.mark.parametrize(
    ("level", "template_effort"),
    [
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("max", "high"),
    ],
)
def test_glm_on_sends_template_effort_and_top_level(level, template_effort):
    """GLM "on" levels map into the template and mirror the level at top level.

    "max" is rejected upstream in resolve_reasoning; the mapping to "high" here is
    the safety net that keeps it from ever reaching the template.
    """
    fields = reasoning_payload_fields(level, EFFORT_AND_THINKING, "glm")
    assert fields == {
        "chat_template_kwargs": {"reasoning_effort": template_effort},
        "reasoning_effort": level,
    }


def test_glm_thinking_mode_omits_top_level_effort():
    """Mode "thinking" sends only the template kwargs for GLM."""
    fields = reasoning_payload_fields("high", THINKING_ONLY, "glm")
    assert fields == {"chat_template_kwargs": {"reasoning_effort": "high"}}


@pytest.mark.parametrize("level", REASONING_LEVELS)
def test_glm_never_sends_enable_thinking_or_thinking(level):
    """GLM must never receive enable_thinking (harmful) or thinking (ignored)."""
    fields = reasoning_payload_fields(level, EFFORT_AND_THINKING, "glm")
    template_kwargs = fields["chat_template_kwargs"]
    assert "enable_thinking" not in template_kwargs
    assert "thinking" not in template_kwargs


def test_qwen3_off_disables_thinking_via_template():
    """Qwen3 "off" is exactly the enable_thinking: false switch."""
    assert reasoning_payload_fields("off", EFFORT_AND_THINKING, "qwen3") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_qwen3_high_sends_thinking_flag_and_effort():
    """Qwen3 "on" sends the thinking flag plus the top-level effort."""
    assert reasoning_payload_fields("high", EFFORT_AND_THINKING, "qwen3") == {
        "chat_template_kwargs": {"thinking": True},
        "reasoning_effort": "high",
    }


def test_qwen3_thinking_mode_drops_top_level_effort():
    """Mode "thinking" sends only the template kwargs for Qwen3."""
    assert reasoning_payload_fields("high", THINKING_ONLY, "qwen3") == {
        "chat_template_kwargs": {"thinking": True}
    }


def test_default_off_sends_nothing():
    """The default profile has no off switch, so "off" sends nothing."""
    assert reasoning_payload_fields("off", EFFORT_AND_THINKING, "default") == {}


def test_default_high_matches_qwen3_high():
    """The default profile shares Qwen3's "on" field set."""
    assert reasoning_payload_fields("high", EFFORT_AND_THINKING, "default") == {
        "chat_template_kwargs": {"thinking": True},
        "reasoning_effort": "high",
    }


@pytest.mark.parametrize("level", REASONING_LEVELS)
def test_default_never_sends_enable_thinking(level):
    """enable_thinking breaks the ollama coder model, so it is never sent."""
    fields = reasoning_payload_fields(level, EFFORT_AND_THINKING, "default")
    template_kwargs = fields.get("chat_template_kwargs", {})
    assert "enable_thinking" not in template_kwargs


def test_unknown_profile_behaves_as_default():
    """Unknown profile names fall back to the default field set."""
    assert reasoning_payload_fields("high", EFFORT_AND_THINKING, "nonsense") == (
        reasoning_payload_fields("high", EFFORT_AND_THINKING, "default")
    )


def test_invalid_level_raises_value_error():
    """A level outside REASONING_LEVELS raises ValueError naming the valid ones."""
    with pytest.raises(ValueError, match="Valid levels"):
        reasoning_payload_fields("extreme", EFFORT_AND_THINKING, "glm")


# --- usage_token_count -----------------------------------------------------


def test_usage_token_count_none_usage_is_none():
    """A missing usage block yields None."""
    assert usage_token_count(None, "reasoning_tokens") is None


def test_usage_token_count_reads_top_level_int():
    """An int at the top level of usage is returned directly."""
    assert usage_token_count({"completion_tokens": 5}, "completion_tokens") == 5


def test_usage_token_count_reads_nested_int():
    """The proxy reports reasoning tokens inside completion_tokens_details."""
    usage = {"completion_tokens_details": {"reasoning_tokens": 7}}
    assert usage_token_count(usage, "reasoning_tokens") == 7


def test_usage_token_count_ignores_nested_non_int():
    """A non-int nested value does not count."""
    usage = {"completion_tokens_details": {"reasoning_tokens": "7"}}
    assert usage_token_count(usage, "reasoning_tokens") is None


def test_usage_token_count_missing_key_is_none():
    """A key absent from both places yields None."""
    assert usage_token_count({"completion_tokens": 5}, "reasoning_tokens") is None


def test_usage_token_count_top_level_int_beats_nested():
    """An int at the top level takes precedence over a nested value."""
    usage = {"reasoning_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 9}}
    assert usage_token_count(usage, "reasoning_tokens") == 3


def test_usage_token_count_nested_wins_over_non_int_top_level():
    """A non-int top-level value falls through to the nested int."""
    usage = {
        "reasoning_tokens": "12",
        "completion_tokens_details": {"reasoning_tokens": 12},
    }
    assert usage_token_count(usage, "reasoning_tokens") == 12
