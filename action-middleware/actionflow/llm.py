"""LLM access: OpenAI-compatible providers, calls, streaming, API keys.

Module state (MODE, ready, provider, model, fallback_*) is set once by
init() at startup; read it as `llm.MODE` etc. — never `from llm import MODE`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from actionflow.config import CONFIG


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    base_url: str | None      # None → OpenAI's default endpoint
    default_model: str
    free: bool = False
    local: bool = False       # runs on this machine, no API key
    key_url: str = ""


# Defaults checked September 2026. Every model below is on a free tier
# except OpenAI's; override any of them with llm.model in config.yaml.
PROVIDERS: dict[str, Provider] = {p.name: p for p in [
    Provider("groq", "Groq — free, fastest", "https://api.groq.com/openai/v1",
             "openai/gpt-oss-120b", free=True, key_url="https://console.groq.com/keys"),
    Provider("gemini", "Google Gemini — free, strong multilingual",
             "https://generativelanguage.googleapis.com/v1beta/openai/",
             "gemini-3.5-flash-lite", free=True, key_url="https://aistudio.google.com/apikey"),
    Provider("cerebras", "Cerebras — free, very fast", "https://api.cerebras.ai/v1",
             "gpt-oss-120b", free=True, key_url="https://cloud.cerebras.ai"),
    Provider("openrouter", "OpenRouter — free models", "https://openrouter.ai/api/v1",
             "qwen/qwen3.8-27b:free", free=True, key_url="https://openrouter.ai/keys"),
    Provider("openai", "OpenAI — paid", None, "gpt-6-luna",
             key_url="https://platform.openai.com/api-keys"),
    Provider("ollama", "Ollama — local", "http://localhost:11434/v1", "qwen3.5:9b", local=True),
    Provider("lmstudio", "LM Studio — local", "http://localhost:1234/v1", "local-model", local=True),
]}

RETIRED_PROVIDERS = {
    "github": "GitHub Models was retired on 2026-07-30",
}

# Model IDs that were removed upstream → current replacement
RETIRED_MODELS = {
    "gemini-2.0-flash": "gemini-3.5-flash-lite",
    "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
}


class LLMError(Exception):
    """Raised when every configured LLM provider failed for a request."""


# ── State (set by init) ──────────────────────────────────────
MODE = "mock"                 # "live" | "mock"
ready = False
client = None
provider = ""
model = ""
fallback_ready = False
fallback_client = None
fallback_provider = ""
fallback_model = ""
last_provider_used = ""

# Hook for warnings (main.py points this at TUI.warn)
on_warning: Callable[[str], None] = lambda message: None


# ============================================================
# API keys — env var → config.yaml → system keychain
# ============================================================

KEYRING_SERVICE = "ActionFlow"
KEY_ENV_VARS = {"llm": "ACTIONFLOW_API_KEY", "image": "ACTIONFLOW_IMAGE_API_KEY"}


def secret_get(account: str) -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, account) or ""
    except Exception:
        return ""


def secret_set(account: str, value: str) -> None:
    """Store a secret in the macOS Keychain / Linux Secret Service. Raises on failure."""
    import keyring
    keyring.set_password(KEYRING_SERVICE, account, value)


def resolve_api_key(kind: str, provider_name: str, config_value: str = "") -> str:
    """API key for `kind` ("llm" | "image") and provider, or ""."""
    env_value = os.environ.get(KEY_ENV_VARS[kind], "").strip()
    if env_value:
        return env_value
    if (config_value or "").strip():
        return config_value.strip()
    if provider_name:
        return secret_get(f"{kind}:{provider_name.strip().lower()}")
    return ""


# ============================================================
# Clients
# ============================================================

def make_client(provider_name: str, api_key: str, model_name: str = "", *,
                settings: dict | None = None):
    """(client, resolved_model). Raises ValueError for unknown providers."""
    from openai import OpenAI

    if provider_name in RETIRED_PROVIDERS:
        raise ValueError(f"{RETIRED_PROVIDERS[provider_name]} — choose another provider")
    info = PROVIDERS.get(provider_name)
    llm_cfg = settings if settings is not None else CONFIG.get("llm", {})
    base_url = (llm_cfg.get("base_url") or "").strip() or (info.base_url if info else None)
    if info is None and not base_url:
        raise ValueError(f"Unknown LLM provider '{provider_name}' (set llm.base_url for custom endpoints)")
    if info is not None and info.local:
        api_key = api_key or provider_name  # the SDK requires a non-empty key
    resolved = model_name or (info.default_model if info else "")
    if resolved in RETIRED_MODELS:
        on_warning(f"Model {resolved} was retired upstream — using {RETIRED_MODELS[resolved]}")
        resolved = RETIRED_MODELS[resolved]
    timeout = float(llm_cfg.get("timeout", 60))
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1), resolved


def init(primary_key: str | None = None) -> None:
    """Initialise primary + fallback clients from CONFIG. Sets MODE='live' on success."""
    global client, ready, provider, model, MODE
    global fallback_client, fallback_ready, fallback_provider, fallback_model

    client, ready, provider, model, MODE = None, False, "", "", "mock"
    fallback_client, fallback_ready, fallback_provider, fallback_model = None, False, "", ""
    llm_cfg = CONFIG.get("llm", {})
    name = (llm_cfg.get("provider") or "").strip().lower()
    info = PROVIDERS.get(name)
    key = primary_key if primary_key is not None else resolve_api_key("llm", name, llm_cfg.get("api_key", ""))
    if not name or (not key and not (info and info.local)):
        return
    try:
        client, model = make_client(name, key, (llm_cfg.get("model") or "").strip())
    except ImportError:
        on_warning("openai package not installed. Run: pip install -r requirements.txt")
        return
    except Exception as exc:
        on_warning(f"LLM setup failed for {name}: {exc}")
        return
    provider, ready, MODE = name, True, "live"

    fb_cfg = llm_cfg.get("fallback") if isinstance(llm_cfg.get("fallback"), dict) else {}
    fb_name = (fb_cfg.get("provider") or "").strip().lower()
    if fb_name and fb_name != name:
        fb_key = (fb_cfg.get("api_key") or "").strip() or secret_get(f"llm:{fb_name}")
        if not fb_key and not PROVIDERS.get(fb_name, Provider("", "", None, "")).local:
            on_warning(f"Fallback provider {fb_name} has no API key — run: main.py --set-key {fb_name}")
            return
        try:
            fallback_client, fallback_model = make_client(
                fb_name, fb_key, (fb_cfg.get("model") or "").strip(), settings=fb_cfg)
            fallback_provider, fallback_ready = fb_name, True
        except Exception as exc:
            on_warning(f"Fallback LLM setup failed for {fb_name}: {exc}")


# ============================================================
# Requests
# ============================================================

def request_options(provider_name: str, model_name: str) -> dict:
    """Provider/model-specific extras. Text transforms need speed, not deep
    reasoning, so thinking is turned off or down wherever the API allows."""
    m = model_name.lower()
    extra: dict = {}
    if provider_name == "groq":
        if m.startswith("openai/gpt-oss"):
            extra = {"reasoning_effort": "low", "include_reasoning": False}
        elif "qwen" in m:
            extra = {"reasoning_effort": "none", "reasoning_format": "hidden"}
    elif provider_name == "cerebras" and "gpt-oss" in m:
        extra = {"reasoning_effort": "low"}
    elif provider_name == "gemini":
        extra = {"reasoning_effort": "none" if "2.5-flash" in m else "low"}
    elif provider_name == "openrouter":
        extra = {"reasoning": {"effort": "low", "exclude": True}}
    settings = CONFIG.get("llm", {})
    fb = settings.get("fallback")
    fb = fb if isinstance(fb, dict) else {}
    if provider_name == fb.get("provider") and provider_name != settings.get("provider"):
        settings = fb
    user_extra = settings.get("request_options")
    if isinstance(user_extra, dict):
        extra.update(user_extra)
    return extra


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove <think>…</think> blocks some open models put in the answer."""
    return tidy(_THINK_RE.sub("", text))


def tidy(text: str) -> str:
    """Final cleanup of an answer: no trailing spaces (Markdown line breaks)."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _create(client_obj, provider_name: str, model_name: str, prompt: str,
            max_tokens: int, temperature: float, stream: bool):
    """chat.completions.create, retrying once without extras if the API
    rejects a provider-specific parameter."""
    from openai import BadRequestError

    kwargs = dict(model=model_name, messages=[{"role": "user", "content": prompt}],
                  max_tokens=max_tokens, temperature=temperature, stream=stream)
    extra = request_options(provider_name, model_name)
    try:
        return client_obj.chat.completions.create(**kwargs, extra_body=extra or None)
    except BadRequestError:
        if not extra:
            raise
        on_warning(f"{provider_name} rejected extra options — retrying without")
        return client_obj.chat.completions.create(**kwargs)


def _settings(max_tokens: int | None, temperature: float | None) -> tuple[int, float]:
    llm_cfg = CONFIG.get("llm", {})
    return (int(max_tokens if max_tokens is not None else llm_cfg.get("max_tokens", 2048)),
            float(temperature if temperature is not None else llm_cfg.get("temperature", 0.7)))


def _candidates(model_override: str) -> list[tuple]:
    out = [(client, provider, model_override or model)]
    if fallback_ready and fallback_client:
        out.append((fallback_client, fallback_provider, fallback_model))
    return out


def mock_response(prompt: str) -> str:
    """Placeholder used only when no provider is configured."""
    lines = prompt.strip().split("\n")
    return f"[Mock Mode] {(lines[-1] if lines else prompt)[:120]}"


def call(prompt: str, model_override: str = "", *, max_tokens: int | None = None,
         temperature: float | None = None) -> str:
    """Complete a prompt (primary, then fallback). Raises LLMError when every
    provider fails — callers must never paste a placeholder over user text."""
    global last_provider_used
    if not ready or not client:
        last_provider_used = "mock"
        return mock_response(prompt)

    max_tokens, temperature = _settings(max_tokens, temperature)
    last_exc: Exception | None = None
    for i, (cl, prov, mdl) in enumerate(_candidates(model_override)):
        try:
            if i:
                on_warning(f"Retrying with fallback ({prov})...")
            response = _create(cl, prov, mdl, prompt, max_tokens, temperature, stream=False)
            text = strip_thinking(response.choices[0].message.content or "")
            if not text:
                raise LLMError("empty response")
            last_provider_used = prov if not i else f"{prov} (fallback)"
            return text
        except Exception as exc:
            on_warning(f"LLM ({prov}) failed: {type(exc).__name__}")
            last_exc = exc
    last_provider_used = ""
    raise LLMError(str(last_exc)[:200]) from last_exc


def stream(prompt: str, model_override: str = "") -> Iterator[str]:
    """Yield answer text as it arrives. Falls back to the next provider only
    before the first chunk; a failure mid-answer raises LLMError."""
    global last_provider_used
    if not ready or not client:
        raise LLMError("No LLM configured — set llm.provider in config.yaml")

    max_tokens, temperature = _settings(None, None)
    last_exc: Exception | None = None
    for i, (cl, prov, mdl) in enumerate(_candidates(model_override)):
        produced = False
        try:
            response = _create(cl, prov, mdl, prompt, max_tokens, temperature, stream=True)
            for delta in _without_thinking(
                    c.choices[0].delta.content for c in response if c.choices):
                produced = True
                yield delta
            if not produced:
                raise LLMError("empty response")
            last_provider_used = prov if not i else f"{prov} (fallback)"
            return
        except Exception as exc:
            if produced:
                raise LLMError(f"connection lost mid-response: {exc}"[:200]) from exc
            on_warning(f"LLM ({prov}) failed: {type(exc).__name__}")
            last_exc = exc
    raise LLMError(str(last_exc)[:200]) from last_exc


def _without_thinking(deltas: Iterator[str | None]) -> Iterator[str]:
    """Drop a leading <think>…</think> block from a stream of text deltas."""
    buffer, state = "", "start"  # start → thinking → text
    for delta in deltas:
        if not delta:
            continue
        if state == "text":
            yield delta
            continue
        buffer += delta
        if state == "start":
            head = buffer.lstrip()
            if not head or (len(head) < len("<think>") and "<think>".startswith(head)):
                continue  # can't tell yet
            if not head.startswith("<think>"):
                state, buffer = "text", ""
                yield head
                continue
            state = "thinking"
        end = buffer.find("</think>")
        if end >= 0:
            rest = buffer[end + len("</think>"):].lstrip()
            state, buffer = "text", ""
            if rest:
                yield rest
    if state == "start" and buffer.strip():
        yield buffer.strip()


def classify(text: str, commands: dict) -> dict | None:
    """Ask the LLM which command fits `text`: {"name", "payload", "confidence"} or None."""
    if MODE != "live" or not ready:
        return None
    cmd_list = "\n".join(f"- {name}: {cmd.get('description', '')}" for name, cmd in commands.items())
    prompt = (
        f"Classify the following text into one of these commands:\n{cmd_list}\n\n"
        f"Text: \"{text}\"\n\n"
        f"Reply with ONLY the command name and your confidence score (0.0-1.0), "
        f"separated by a colon. Example: summarize:0.85\n"
        f"If none match, reply \"unknown:0.0\"."
    )
    try:
        # Room for reasoning tokens: reasoning models count them against max_tokens
        result = call(prompt, max_tokens=300, temperature=0.0).strip().lower()
    except LLMError:
        return None
    name, _, conf = result.partition(":")
    name = name.strip()
    try:
        confidence = float(conf.strip()) if conf else 0.5
    except ValueError:
        confidence = 0.5
    if name in commands and name != "unknown":
        return {"name": name, "payload": text, "confidence": confidence}
    return None


def ping(client_obj, provider_name: str, model_name: str) -> float:
    """Tiny request to validate key + model. Returns latency in seconds; raises on failure."""
    started = time.time()
    response = _create(client_obj, provider_name, model_name, "Reply with the single word: OK",
                       max_tokens=200, temperature=0.0, stream=False)
    if not strip_thinking(response.choices[0].message.content or ""):
        raise LLMError("empty response")
    return time.time() - started


def list_models(client_obj) -> list[str]:
    return sorted(m.id for m in client_obj.models.list())
