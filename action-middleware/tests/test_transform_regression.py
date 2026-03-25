from actionflow.core.llm.transform_executor import execute_transform_command


def _mapped_llm(expected_map: dict[str, str]):
    def _call(prompt: str) -> str:
        for key, value in expected_map.items():
            if key in prompt:
                return value
        raise AssertionError(f"No canned response for prompt: {prompt}")

    return _call


def test_regression_suite_core_cases():
    llm = _mapped_llm(
        {
            "Доброе утро": "おはようございます。",
            "Алдияр гений": "Aldiyar is a genius.",
            "Алдияр Гений!": "Алдияр действительно заслуживает такой высокой оценки.",
            "Данил еблан": "Данил ведет себя крайне грубо и некорректно.",
            "Очень длинный абзац": "Короткое резюме.",
            "plain paragraph": "Короткий заголовок",
            "rough notes": "Subject: Sync update\n\nHello,\nHere is the update.",
            "meeting text": "- Подготовить отчет\n- Созвониться с командой",
            "prose text": "- Первый пункт\n- Второй пункт",
            "привет братан": "Здравствуйте, коллеги.",
        }
    )

    assert execute_transform_command("trans", "JA: Доброе утро", llm).output == "おはようございます。"
    assert execute_transform_command("trans", "EN: Алдияр гений", llm).output == "Aldiyar is a genius."
    assert execute_transform_command("rewrite", "Алдияр Гений!", llm).output == "Алдияр действительно заслуживает такой высокой оценки."
    assert "грубо" in execute_transform_command("rewrite", "Данил еблан", llm).output
    assert execute_transform_command("summarize", "Очень длинный абзац", llm).output == "Короткое резюме."
    assert execute_transform_command("title", "plain paragraph", llm).output == "Короткий заголовок"
    assert execute_transform_command("email", "rough notes", llm).output.startswith("Subject:")
    assert execute_transform_command("todo", "meeting text", llm).output.startswith("- ")
    assert execute_transform_command("bullets", "prose text", llm).output.startswith("- ")
    assert execute_transform_command("tone", "formal: привет братан", llm).output == "Здравствуйте, коллеги."
