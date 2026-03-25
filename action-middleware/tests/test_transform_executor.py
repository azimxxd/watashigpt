from actionflow.core.llm.transform_executor import execute_transform_command


def _sequenced_llm(responses: list[str]):
    state = {"index": 0}

    def _call(prompt: str) -> str:
        idx = min(state["index"], len(responses) - 1)
        state["index"] += 1
        return responses[idx]

    _call.calls = state
    return _call


def test_translate_returns_only_translation():
    llm = _sequenced_llm(["Here is the translation: おはようございます。"])
    result = execute_transform_command("trans", "JA: Доброе утро", llm)
    assert result.output == "おはようございます。"


def test_translate_retries_when_output_is_wrong_language():
    llm = _sequenced_llm(["Good morning.", "おはようございます。"])
    result = execute_transform_command("trans", "JA: Доброе утро", llm)
    assert result.output == "おはようございます。"
    assert llm.calls["index"] == 2


def test_gitcommit_returns_commit_style_message():
    llm = _sequenced_llm(["Richard", "feat: rename CLI entrypoint"])
    result = execute_transform_command("gitcommit", "rename main wrapper", llm)
    assert result.output == "feat: rename CLI entrypoint"
    assert llm.calls["index"] == 2


def test_rewrite_preserves_source_language_and_retries():
    llm = _sequenced_llm(["Hello Aldiyar", "Алдияр действительно заслуживает такой высокой оценки."])
    result = execute_transform_command("rewrite", "Алдияр Гений!", llm)
    assert result.output == "Алдияр действительно заслуживает такой высокой оценки."
    assert llm.calls["index"] == 2


def test_rewrite_retries_when_change_is_only_punctuation_for_short_input():
    llm = _sequenced_llm(
        [
            "Программирование - это легко.",
            "Программирование может быть гораздо проще, чем кажется на первый взгляд, особенно если изучать его последовательно и на понятных примерах.",
        ]
    )
    result = execute_transform_command("rewrite", "программирование это легко", llm)
    assert "кажется" in result.output
    assert llm.calls["index"] == 2


def test_rewrite_retries_when_short_english_input_is_too_weak():
    llm = _sequenced_llm(
        [
            "Programming is easy.",
            "Programming can be much easier to learn and practice than many people expect, especially when concepts are explained clearly and approached step by step.",
        ]
    )
    result = execute_transform_command("rewrite", "Programming is easy", llm)
    assert "step by step" in result.output
    assert llm.calls["index"] == 2


def test_rewrite_softens_rough_phrasing():
    llm = _sequenced_llm(["Данил ведет себя крайне грубо и некорректно."])
    result = execute_transform_command("rewrite", "Данил еблан", llm)
    assert result.output == "Данил ведет себя крайне грубо и некорректно."


def test_summarize_returns_summary_only():
    llm = _sequenced_llm(["Here's the summary: Короткое резюме текста."])
    result = execute_transform_command("summarize", "Очень длинный текст, который нужно сократить.", llm)
    assert result.output == "Короткое резюме текста."


def test_title_returns_only_title():
    llm = _sequenced_llm(["Here is the title: Как запустить ActionFlow на Windows"])
    result = execute_transform_command("title", "Текст о запуске ActionFlow на Windows.", llm)
    assert result.output == "Как запустить ActionFlow на Windows"


def test_email_returns_only_email():
    llm = _sequenced_llm(["Sure, here is the email:\nТема: Обновление\n\nПривет,\nВсе готово.\n"])
    result = execute_transform_command("email", "Нужно написать письмо про обновление статуса.", llm)
    assert result.output.startswith("Тема: Обновление")


def test_email_retries_when_output_mixes_languages():
    llm = _sequenced_llm(
        [
            "Subject: Update\n\nПривет, team.\nВсе готово.",
            "Тема: Обновление\n\nПривет,\nВсе готово.",
        ]
    )
    result = execute_transform_command("email", "Нужно написать письмо про обновление статуса.", llm)
    assert result.output.startswith("Тема:")
    assert llm.calls["index"] == 2


def test_explain_retries_when_output_is_just_a_near_echo():
    llm = _sequenced_llm(
        [
            "HTTP cache stores HTTP cache.",
            "HTTP cache keeps a saved copy of data so an app can load it faster instead of fetching everything again.",
        ]
    )
    result = execute_transform_command("explain", "HTTP cache", llm)
    assert "faster" in result.output
    assert llm.calls["index"] == 2


def test_haiku_returns_short_multiline_poem():
    llm = _sequenced_llm(["Morning rain drifts slow\nQuiet windows learn the light\nTea waits by the code"])
    result = execute_transform_command("haiku", "A calm morning while coding", llm)
    assert len(result.output.splitlines()) == 3


def test_fill_uses_explicit_assignments_without_llm_call():
    llm = _sequenced_llm(["unused"])
    result = execute_transform_command(
        "fill",
        "name=Aldiyar|role=founder: My name is {{name}} and I am a {{role}}",
        llm,
    )
    assert result.output == "My name is Aldiyar and I am a founder"
    assert llm.calls["index"] == 0


def test_invalid_model_output_triggers_retry():
    llm = _sequenced_llm(["Sure, I'd be happy to help.", "- Found risk\n- Missing tests"])
    result = execute_transform_command("review", "def foo(): pass", llm)
    assert result.output == "- Found risk\n- Missing tests"
    assert llm.calls["index"] == 2
