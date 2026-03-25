from actionflow.core.llm_ops import build_rewrite_prompt, clean_rewrite_output, strip_matching_prefix


def test_rw_payload_strips_prefix_before_prompting_model():
    assert strip_matching_prefix("RW: Алдияр Гений", ["RW:", "REWRITE:"]) == "Алдияр Гений"


def test_rewrite_prompt_is_transform_only_and_preserves_language():
    prompt = build_rewrite_prompt("Алдияр Гений")
    assert "SAME LANGUAGE as the source text" in prompt
    assert "A minimal punctuation-only fix is not enough" in prompt
    assert "noticeably better written than the input" in prompt
    assert "Do not answer as a chat assistant" in prompt
    assert prompt.endswith("Алдияр Гений")


def test_rewrite_prompt_supports_strength_and_context():
    prompt = build_rewrite_prompt(
        "Programming is easy",
        strength="normal",
        source_language="en",
        app_context="docs",
    )
    assert "Use a normal rewrite strength" in prompt
    assert "Detected source language: en" in prompt
    assert "Source application context: docs" in prompt


def test_rewrite_output_strips_assistant_style_preamble():
    cleaned = clean_rewrite_output("Here's the rewritten text: Алдияр действительно заслуживает такой высокой оценки.", "Алдияр Гений")
    assert cleaned == "Алдияр действительно заслуживает такой высокой оценки."


def test_rewrite_output_rejects_unexpected_language_switch():
    try:
        clean_rewrite_output("Hello Aldiyar", "Алдияр Гений")
    except ValueError as exc:
        assert "changed script/language" in str(exc)
    else:
        raise AssertionError("Expected rewrite validation to reject language switch")
