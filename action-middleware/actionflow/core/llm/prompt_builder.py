from __future__ import annotations

from .command_specs import ParsedCommandInput, TransformCommandSpec


BASE_RULES = [
    "You are a deterministic text transformation engine.",
    "Do not respond conversationally.",
    "Do not add commentary, disclaimers, greetings, introductions, or assistant-style framing.",
    "Return only the final transformed output.",
]


def _rewrite_rules(parsed: ParsedCommandInput) -> list[str]:
    strength = parsed.args.get("strength", "strong").strip().lower() or "strong"
    language_hint = parsed.args.get("source_language", "").strip()
    app_context = parsed.args.get("app_context", "").strip()

    rules = [
        "You are a text rewriting engine, not a chatbot.",
        "Rewrite the provided text in the SAME LANGUAGE as the source text.",
        "Preserve the original meaning, but improve clarity, fluency, structure, tone, and wording.",
        "If the source text is short, rough, simplistic, or poorly phrased, expand it slightly into a more natural, polished, and well-formed sentence while keeping the same core meaning.",
        "A minimal punctuation-only fix is not enough if the source is clearly underwritten.",
        "The output should be noticeably better written than the input.",
        "Do not translate unless explicitly requested.",
        "Do not add commentary, explanations, greetings, disclaimers, markdown, or meta-text.",
        "Return ONLY the rewritten text.",
    ]

    if strength == "light":
        rules.append("Use a light rewrite strength: polish wording while keeping the text close to the source.")
    elif strength == "normal":
        rules.append("Use a normal rewrite strength: noticeably improve wording and structure without over-expanding.")
    else:
        rules.append("Use a strong rewrite strength: substantially improve wording and fluency, and enrich sparse input into one polished sentence when needed.")

    if language_hint:
        rules.append(f"Detected source language: {language_hint}. Keep the output in that language.")
    if app_context:
        rules.append(f"Source application context: {app_context}. Use that only as light context, not as a reason to change the meaning.")
    return rules


def build_prompt(spec: TransformCommandSpec, parsed: ParsedCommandInput) -> str:
    rules = list(BASE_RULES)
    if spec.preserve_language:
        rules.append("Preserve the source language unless the command explicitly requests a target language.")

    if spec.name in {"translate", "trans"}:
        rules.append(f"Translate the text into {parsed.args.get('target_label', parsed.args.get('target_code', 'the requested language'))}.")
        rules.append("Return only the translation.")
    elif spec.name == "rewrite":
        rules.extend(_rewrite_rules(parsed))
    elif spec.name == "explain":
        rules.append("Explain what the source means in clearer, simpler language.")
        rules.append("Do not merely paraphrase the source or repeat it with tiny edits.")
        rules.append("If the source is short, expand the meaning enough to be useful.")
        rules.append("If the source is complex, simplify it into a direct explanation.")
        rules.append("Return only the explanation.")
    elif spec.name == "email":
        rules.append("Turn the source notes into a polished email draft.")
        rules.append("Keep the entire email in a single language that matches the source text unless the prompt explicitly asks otherwise.")
        rules.append("Do not mix languages in the subject line or body.")
        rules.append("If useful, include a first-line subject label in the same language as the body, then a blank line, then the message.")
        rules.append("Return only the final email draft.")
    elif spec.name == "haiku":
        rules.append("Write a short haiku-style poem inspired by the source text.")
        rules.append("Prefer three short lines and keep the poem concise.")
        rules.append("Do not add commentary before or after the poem.")
        rules.append("Return only the poem.")
    elif spec.name == "tone":
        rules.append(f"Change only the tone to: {parsed.args.get('style', 'requested style')}.")
        rules.append("Preserve meaning, facts, and intent.")
    elif spec.name == "fill":
        if parsed.args:
            assignments = ", ".join(f"{k}={v}" for k, v in parsed.args.items())
            rules.append(f"Apply these explicit placeholder values first: {assignments}.")
        rules.append("Fill placeholders like {{name}} directly inside the template text.")
        rules.append("Do not leave provided placeholders unresolved or return the unchanged template when values were supplied.")
        rules.append("Return only the completed text.")
    else:
        rules.append(spec.task_description)
        rules.append(spec.output_schema)

    instructions = "\n".join(f"- {rule}" for rule in rules)
    return f"{instructions}\n\nSource text:\n{parsed.payload.strip()}"


def build_retry_prompt(spec: TransformCommandSpec, parsed: ParsedCommandInput, previous_output: str, reason: str) -> str:
    prompt = build_prompt(spec, parsed)
    retry_instruction = "Try again and follow the rules exactly."
    if spec.name == "rewrite":
        retry_instruction = (
            "Try again and follow the rules exactly. Make the rewrite more substantial, "
            "more polished, and clearly stronger than a punctuation-only fix while preserving meaning and language."
        )
    return (
        f"{prompt}\n\n"
        f"Your previous output was invalid.\n"
        f"Reason: {reason}\n"
        f"Invalid output:\n{previous_output}\n\n"
        f"{retry_instruction}"
    )
