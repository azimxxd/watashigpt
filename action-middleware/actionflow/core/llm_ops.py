from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable

import yaml

from actionflow.app.paths import secrets_path


PROVIDER_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
    "github": "https://models.inference.ai.azure.com",
}

PROVIDER_DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "github": "gpt-4o-mini",
}

LLM_STATE_READY = "ready"
LLM_STATE_NEEDS_SETUP = "needs_setup"
LLM_STATE_MOCK = "mock"

SECRETS_PATH = secrets_path()


@dataclass(frozen=True)
class LLMResolution:
    state: str
    provider: str
    model: str
    api_key: str
    explicit_mock: bool = False
    source: str = ""


@dataclass(frozen=True)
class LLMSetupChoice:
    action: str
    provider: str = ""
    api_key: str = ""
    model: str = ""


def init_llm_client(provider: str, api_key: str, model: str):
    try:
        from openai import OpenAI
    except ImportError:
        return None, ""

    resolved_model = model or PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")
    if provider == "openai":
        return OpenAI(api_key=api_key), resolved_model
    if provider in PROVIDER_BASE_URLS:
        return OpenAI(api_key=api_key, base_url=PROVIDER_BASE_URLS[provider]), resolved_model
    return None, ""


def load_llm_secrets(path: Path | None = None) -> dict[str, Any]:
    secrets_path = path or SECRETS_PATH
    if not secrets_path.exists():
        return {}
    try:
        with open(secrets_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_llm_secrets(api_key: str, path: Path | None = None) -> None:
    secrets_path = path or SECRETS_PATH
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"llm": {"api_key": api_key}}
    with open(secrets_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, default_flow_style=False, allow_unicode=True)
    try:
        os.chmod(secrets_path, 0o600)
    except Exception:
        pass


def redact_llm_settings(data: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(data)
    llm = dict(sanitized.get("llm", {})) if isinstance(sanitized.get("llm", {}), dict) else {}
    if "api_key" in llm and llm["api_key"]:
        llm["api_key"] = "***"
    fallback = dict(llm.get("fallback", {})) if isinstance(llm.get("fallback", {}), dict) else {}
    if "api_key" in fallback and fallback["api_key"]:
        fallback["api_key"] = "***"
    if fallback:
        llm["fallback"] = fallback
    sanitized["llm"] = llm
    return sanitized


def resolve_llm_state(
    config: dict[str, Any],
    env: dict[str, str] | None = None,
    secrets: dict[str, Any] | None = None,
    force_mock: bool = False,
) -> LLMResolution:
    env = env or {}
    secrets = secrets or {}
    llm_cfg = config.get("llm", {}) if isinstance(config.get("llm", {}), dict) else {}
    provider = str(llm_cfg.get("provider", "")).strip().lower()
    model = str(llm_cfg.get("model", "")).strip() or PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")

    env_mode = str(env.get("ACTIONFLOW_LLM_MODE", "")).strip().lower()
    cfg_mode = str(llm_cfg.get("mode", "auto")).strip().lower()
    explicit_mock = force_mock or env_mode == "mock" or cfg_mode == "mock"
    if explicit_mock:
        return LLMResolution(
            state=LLM_STATE_MOCK,
            provider=provider,
            model=model,
            api_key="",
            explicit_mock=True,
            source="explicit",
        )

    api_key = str(env.get("ACTIONFLOW_API_KEY", "")).strip()
    source = "env" if api_key else ""
    if not api_key:
        api_key = str(llm_cfg.get("api_key", "")).strip()
        source = "config" if api_key else source
    if not api_key:
        secrets_llm = secrets.get("llm", {}) if isinstance(secrets.get("llm", {}), dict) else {}
        api_key = str(secrets_llm.get("api_key", "")).strip()
        source = "secrets" if api_key else source

    if provider and api_key:
        return LLMResolution(
            state=LLM_STATE_READY,
            provider=provider,
            model=model,
            api_key=api_key,
            explicit_mock=False,
            source=source,
        )

    return LLMResolution(
        state=LLM_STATE_NEEDS_SETUP,
        provider=provider,
        model=model,
        api_key="",
        explicit_mock=False,
        source="",
    )


def maybe_prompt_llm_setup(
    resolution: LLMResolution,
    prompt_user: Callable[[], LLMSetupChoice],
    persist_config: Callable[[str, str], None],
    persist_secret: Callable[[str], None],
    init_backend: Callable[[], bool],
) -> str:
    if resolution.state in (LLM_STATE_READY, LLM_STATE_MOCK):
        return resolution.state

    choice = prompt_user()
    if choice.action == "cancel":
        return LLM_STATE_NEEDS_SETUP
    if choice.action == "mock":
        persist_config("mock", choice.model or resolution.model)
        return LLM_STATE_MOCK
    if choice.action == "configure" and choice.provider and choice.api_key:
        persist_config(choice.provider, choice.model)
        persist_secret(choice.api_key)
        return LLM_STATE_READY if init_backend() else LLM_STATE_NEEDS_SETUP
    return LLM_STATE_NEEDS_SETUP


def strip_matching_prefix(text: str, prefixes: list[str]) -> str:
    for prefix in sorted(prefixes, key=len, reverse=True):
        if text.upper().startswith(prefix.upper()):
            return text[len(prefix):].lstrip()
    return text


def build_rewrite_prompt(text: str, *, strength: str = "strong", source_language: str = "", app_context: str = "") -> str:
    strength = (strength or "strong").strip().lower()
    strength_rule = {
        "light": "Use a light rewrite strength: keep the result close to the source while improving wording.",
        "normal": "Use a normal rewrite strength: noticeably improve wording and structure without over-expanding.",
    }.get(
        strength,
        "Use a strong rewrite strength: substantially improve wording and fluency, and enrich sparse input into one polished sentence when needed.",
    )
    return (
        "You are a text rewriting engine, not a chatbot.\n"
        "Rewrite only the provided text.\n"
        "Rewrite the text in the SAME LANGUAGE as the source text.\n"
        "Preserve the original meaning.\n"
        "Improve clarity, fluency, structure, tone, and wording.\n"
        "If the source text is short, rough, simplistic, or poorly phrased, expand it slightly into a more natural, polished, and well-formed sentence while keeping the same core meaning.\n"
        "A minimal punctuation-only fix is not enough if the source is clearly underwritten.\n"
        "The output should be noticeably better written than the input.\n"
        "Do not translate.\n"
        "Do not answer as a chat assistant.\n"
        "Do not add greetings, explanations, disclaimers, commentary, framing, markdown, or meta-text.\n"
        f"{strength_rule}\n"
        f"{('Detected source language: ' + source_language + '. Keep the output in that language.\n') if source_language else ''}"
        f"{('Source application context: ' + app_context + '. Use that only as light context, not as a reason to change the meaning.\n') if app_context else ''}"
        "Return ONLY the final rewritten text.\n\n"
        f"Text to rewrite:\n{text.strip()}"
    )


_ASSISTANT_PREAMBLE_RE = re.compile(
    r"^\s*(?:sure|certainly|of course|here(?:'s| is)|rewritten text|rewrite|result|hello|hi)\b[:!,\s-]*",
    re.IGNORECASE,
)


def dominant_script(text: str) -> str:
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    if cyrillic > latin and cyrillic > 0:
        return "cyrillic"
    if latin > cyrillic and latin > 0:
        return "latin"
    return "other"


def clean_rewrite_output(result: str, original_text: str) -> str:
    cleaned = result.strip().strip('"').strip("'").strip()

    while True:
        updated = _ASSISTANT_PREAMBLE_RE.sub("", cleaned).strip()
        if updated == cleaned:
            break
        cleaned = updated

    cleaned = re.sub(
        r"^\s*(?:(?:here(?:'s| is)\s+)?(?:the\s+)?)?rewritten(?:\s+text|\s+version)?\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    source_script = dominant_script(original_text)
    output_script = dominant_script(cleaned)
    if source_script in {"cyrillic", "latin"} and output_script not in {source_script, "other"}:
        raise ValueError("Rewrite output changed script/language unexpectedly")
    if not cleaned:
        raise ValueError("Rewrite output is empty after cleanup")
    return cleaned
