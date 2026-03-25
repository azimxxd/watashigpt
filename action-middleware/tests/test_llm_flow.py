from actionflow.core.llm_ops import (
    LLMResolution,
    LLMSetupChoice,
    LLM_STATE_MOCK,
    LLM_STATE_NEEDS_SETUP,
    LLM_STATE_READY,
    maybe_prompt_llm_setup,
    redact_llm_settings,
    resolve_llm_state,
)


def test_llm_state_ready():
    config = {"llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"}}
    secrets = {"llm": {"api_key": "secret-key"}}
    resolved = resolve_llm_state(config, env={}, secrets=secrets)
    assert resolved.state == LLM_STATE_READY
    assert resolved.provider == "groq"
    assert resolved.api_key == "secret-key"


def test_llm_state_needs_setup():
    config = {"llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"}}
    resolved = resolve_llm_state(config, env={}, secrets={})
    assert resolved.state == LLM_STATE_NEEDS_SETUP


def test_mock_mode_explicit_only():
    config = {"llm": {"provider": "groq", "model": "llama-3.3-70b-versatile", "mode": "mock"}}
    resolved = resolve_llm_state(config, env={}, secrets={"llm": {"api_key": "secret"}})
    assert resolved.state == LLM_STATE_MOCK
    assert resolved.explicit_mock is True


def test_missing_key_prompts_user():
    calls = {"prompted": 0}

    def prompt_user():
        calls["prompted"] += 1
        return LLMSetupChoice(action="cancel")

    state = maybe_prompt_llm_setup(
        LLMResolution(state=LLM_STATE_NEEDS_SETUP, provider="", model="", api_key=""),
        prompt_user,
        lambda provider, model: None,
        lambda api_key: None,
        lambda: False,
    )
    assert state == LLM_STATE_NEEDS_SETUP
    assert calls["prompted"] == 1


def test_setup_then_retry_command():
    saved = {}

    def prompt_user():
        return LLMSetupChoice(
            action="configure",
            provider="openai",
            api_key="top-secret",
            model="gpt-4o-mini",
        )

    def persist_config(provider: str, model: str):
        saved["provider"] = provider
        saved["model"] = model

    def persist_secret(api_key: str):
        saved["api_key"] = api_key

    state = maybe_prompt_llm_setup(
        LLMResolution(state=LLM_STATE_NEEDS_SETUP, provider="", model="", api_key=""),
        prompt_user,
        persist_config,
        persist_secret,
        lambda: True,
    )
    assert state == LLM_STATE_READY
    assert saved == {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "top-secret",
    }


def test_cancel_setup_does_not_return_placeholder():
    state = maybe_prompt_llm_setup(
        LLMResolution(state=LLM_STATE_NEEDS_SETUP, provider="", model="", api_key=""),
        lambda: LLMSetupChoice(action="cancel"),
        lambda provider, model: None,
        lambda api_key: None,
        lambda: False,
    )
    assert state == LLM_STATE_NEEDS_SETUP


def test_no_secret_leak_in_logs():
    sanitized = redact_llm_settings(
        {
            "llm": {
                "provider": "groq",
                "api_key": "super-secret",
                "fallback": {"provider": "openai", "api_key": "fallback-secret"},
            }
        }
    )
    assert sanitized["llm"]["api_key"] == "***"
    assert sanitized["llm"]["fallback"]["api_key"] == "***"
