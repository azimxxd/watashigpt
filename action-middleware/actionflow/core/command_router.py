from __future__ import annotations


def resolve_prefix(text: str, commands: dict) -> tuple[str, str, dict] | None:
    text_upper = text.upper()
    for name, cmd in commands.items():
        for prefix in cmd.get("prefixes", []):
            if text_upper.startswith(prefix.upper()):
                return name, text[len(prefix):], cmd
    return None


def parse_chain(text: str, commands: dict) -> list[tuple[str, dict]] | None:
    if "|" not in text:
        return None

    steps: list[tuple[str, dict]] = []
    remaining = text
    while True:
        pipe_pos = remaining.find("|")
        if pipe_pos == -1:
            break
        candidate = remaining[:pipe_pos]
        match = resolve_prefix(candidate, commands)
        if not match:
            break
        steps.append((match[0], match[2]))
        remaining = remaining[pipe_pos + 1:]

    if not steps:
        return None

    final_match = resolve_prefix(remaining, commands)
    if not final_match:
        return None

    steps.append((final_match[0], final_match[2]))
    return steps


def extract_chain_payload(text: str, commands: dict) -> str:
    remaining = text
    while True:
        pipe_pos = remaining.find("|")
        if pipe_pos == -1:
            break
        candidate = remaining[:pipe_pos]
        if resolve_prefix(candidate, commands):
            remaining = remaining[pipe_pos + 1:]
        else:
            break
    match = resolve_prefix(remaining, commands)
    return match[1] if match else remaining
