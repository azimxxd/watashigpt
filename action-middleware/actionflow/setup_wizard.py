"""First-run setup in the terminal: pick a provider, enter + verify the key,
store it in the keychain, optionally add a free backup provider."""

from __future__ import annotations

import getpass
import sys

from actionflow import llm
from actionflow.config import CONFIG, save_nested, save_values
from actionflow.tui import TUI


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _choose(options: list[str]) -> int | None:
    if sys.stdin.isatty():
        return TUI.selector_vertical(options)
    for i, option in enumerate(options, 1):
        print(f"  [{i}] {option}")
    raw = _ask(f"  Choice (1-{len(options)}): ")
    return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(options) else None


def _save(values: dict, section: str = "llm") -> None:
    try:
        save_values(section, values)
    except Exception as exc:
        TUI.warn(f"Could not save config.yaml: {exc}")


def _store_key(account: str, key: str, env_var: str) -> None:
    store = "Keychain" if sys.platform == "darwin" else "system keyring"
    answer = _ask(f"  Save the key in {store} so you never re-enter it? [Y/n] ").lower()
    if answer in ("", "y", "yes", "д", "да"):
        try:
            llm.secret_set(account, key)
            print(f"  {TUI.GREEN}✓ Saved to {store}{TUI.RESET}")
            return
        except Exception as exc:
            TUI.warn(f"Could not save to {store}: {exc}")
    print(f"  {TUI.DIM}Tip: add  export {env_var}=<your key>  to your shell profile{TUI.RESET}")


def _configure_provider(name: str, *, current_model: str = "") -> tuple[str, str] | None:
    """Ask for key + model and verify them. Returns (key, model) or None."""
    info = llm.PROVIDERS[name]
    key = ""
    if not info.local:
        if info.key_url:
            print(f"  {TUI.DIM}Get a {'free ' if info.free else ''}key: {TUI.CYAN}{info.key_url}{TUI.RESET}")
        try:
            key = getpass.getpass(f"  {TUI.CYAN}{TUI.BOLD}API key{TUI.RESET} (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            key = ""
        if not key:
            print(f"  {TUI.YELLOW}No key entered.{TUI.RESET}")
            return None

    default_model = current_model or info.default_model
    model = _ask(f"  {TUI.CYAN}{TUI.BOLD}Model{TUI.RESET} {TUI.DIM}[{default_model}]{TUI.RESET}: ") or default_model

    print(f"  {TUI.DIM}Checking connection…{TUI.RESET}", end="", flush=True)
    try:
        client, model = llm.make_client(name, key, model)
        seconds = llm.ping(client, name, model)
        print(f"\r  {TUI.GREEN}✓ {info.label.split(' —')[0]} / {model} works ({seconds:.1f}s){TUI.RESET}   ")
    except Exception as exc:
        print(f"\r  {TUI.RED}✗ Check failed: {str(exc)[:160]}{TUI.RESET}")
        if _ask("  Use it anyway? [y/N] ").lower() not in ("y", "yes", "д", "да"):
            return None
    return key, model


def run_llm_setup() -> None:
    """Interactive LLM setup — skipped when a provider and key already exist."""
    llm_cfg = CONFIG.setdefault("llm", {})
    name = (llm_cfg.get("provider") or "").strip().lower()
    if name in llm.RETIRED_PROVIDERS:
        TUI.warn(f"{llm.RETIRED_PROVIDERS[name]} — please choose another provider")
        name = ""
    info = llm.PROVIDERS.get(name)
    has_key = bool(llm.resolve_api_key("llm", name, llm_cfg.get("api_key", "")))
    if name and (has_key or (info and info.local)):
        return
    if not sys.stdin.isatty():
        return  # login agent / service: never block on input

    print()
    if info:
        TUI.box("LLM Setup", [f"  {TUI.DIM}Provider {TUI.BOLD}{name}{TUI.RESET}{TUI.DIM} is set but has no API key{TUI.RESET}"])
    else:
        TUI.box("LLM Setup", [
            f"  {TUI.DIM}An LLM powers rewrite, translate, summarize and custom instructions.{TUI.RESET}",
            f"  {TUI.DIM}Groq, Gemini, Cerebras and OpenRouter have free tiers — no card needed.{TUI.RESET}",
        ])
        names = list(llm.PROVIDERS)
        choice = _choose([llm.PROVIDERS[n].label for n in names] + ["Skip — built-in commands only"])
        if choice is None or choice >= len(names):
            print(f"  {TUI.DIM}Running without an LLM (mock mode).{TUI.RESET}\n")
            return
        name, info = names[choice], llm.PROVIDERS[names[choice]]

    result = _configure_provider(name, current_model=llm_cfg.get("model", "") if info else "")
    if result is None:
        print(f"  {TUI.DIM}Running without an LLM (mock mode).{TUI.RESET}\n")
        return
    key, model = result
    llm_cfg.update(provider=name, model=model, api_key=key)
    _save({"provider": name, "model": model})
    if key:
        _store_key(f"llm:{name}", key, llm.KEY_ENV_VARS["llm"])
        llm_cfg["api_key"] = key  # in-memory only for this run

    _offer_fallback(name)
    print()


def _offer_fallback(primary: str) -> None:
    llm_cfg = CONFIG["llm"]
    fb = llm_cfg.get("fallback") if isinstance(llm_cfg.get("fallback"), dict) else {}
    if (fb.get("provider") or "").strip():
        return
    others = [n for n, p in llm.PROVIDERS.items() if p.free and n != primary]
    if not others:
        return
    answer = _ask(f"\n  Add a free backup provider (used when {primary} is down or rate-limited)? [y/N] ").lower()
    if answer not in ("y", "yes", "д", "да"):
        return
    choice = _choose([llm.PROVIDERS[n].label for n in others] + ["Cancel"])
    if choice is None or choice >= len(others):
        return
    name = others[choice]
    result = _configure_provider(name)
    if result is None:
        return
    key, model = result
    llm_cfg["fallback"] = {"provider": name, "model": model, "api_key": key}
    try:
        save_nested(["llm", "fallback"], {"provider": name, "model": model})
    except Exception as exc:
        TUI.warn(f"Could not save fallback to config.yaml: {exc}")
    _store_key(f"llm:{name}", key, llm.KEY_ENV_VARS["llm"])


def set_key_cli(name: str) -> None:
    """`main.py --set-key groq` / `--set-key image:pollinations`."""
    account = name.lower() if ":" in name else f"llm:{name.lower()}"
    if account.split(":", 1)[0] not in llm.KEY_ENV_VARS:
        print(f"{TUI.RED}Use a provider name (e.g. groq) or image:<provider>{TUI.RESET}")
        sys.exit(1)
    try:
        value = getpass.getpass(f"API key for {account} (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not value:
        return
    try:
        llm.secret_set(account, value)
        print(f"{TUI.GREEN}✓{TUI.RESET} Saved {account} to {'Keychain' if sys.platform == 'darwin' else 'system keyring'}")
    except Exception as exc:
        print(f"{TUI.RED}Could not save: {exc}{TUI.RESET}")


def check_cli() -> None:
    """`main.py --check`: verify the configured provider(s) and list models."""
    llm.on_warning = TUI.warn
    llm.init()
    if llm.MODE != "live":
        print(f"{TUI.YELLOW}No LLM configured (provider/key missing). Run main.py to set one up.{TUI.RESET}")
        return
    rows = [(llm.provider, llm.client, llm.model)]
    if llm.fallback_ready:
        rows.append((llm.fallback_provider, llm.fallback_client, llm.fallback_model))
    for name, client, model in rows:
        try:
            seconds = llm.ping(client, name, model)
            print(f"{TUI.GREEN}✓{TUI.RESET} {name} / {model}  {TUI.DIM}{seconds:.2f}s{TUI.RESET}")
        except Exception as exc:
            print(f"{TUI.RED}✗{TUI.RESET} {name} / {model}  {str(exc)[:200]}")
        try:
            models = llm.list_models(client)
            print(f"  {TUI.DIM}{len(models)} models available: {', '.join(models[:12])}"
                  f"{' …' if len(models) > 12 else ''}{TUI.RESET}")
        except Exception:
            pass
