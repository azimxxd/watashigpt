from actionflow.core.lookup_ops import (
    build_wiki_result,
    choose_preferred_wiki_title,
    detect_query_language,
    parse_russian_wiktionary_definitions,
    select_safe_definitions,
)


def test_wiki_prefers_specific_non_disambiguation_title():
    preferred = choose_preferred_wiki_title(
        "Python",
        ["Python (disambiguation)", "Python (programming language)", "Pythonidae"],
    )
    assert preferred == "Python (programming language)"


def test_wiki_returns_choices_for_ambiguous_pages():
    result = build_wiki_result(
        "Python",
        {
            "title": "Python",
            "type": "disambiguation",
            "extract": "Python may refer to several topics.",
        },
        {
            "query": {
                "search": [
                    {"title": "Python (programming language)"},
                    {"title": "Pythonidae"},
                ]
            }
        },
    )
    assert result.kind == "choices"
    assert result.choices[0] == "Python (programming language)"


def test_define_filters_out_offensive_or_slang_results():
    selected = select_safe_definitions(
        [
            {
                "meanings": [
                    {
                        "partOfSpeech": "noun",
                        "definitions": [
                            {"definition": "A large nonvenomous snake found in Africa, Asia, and Australia."},
                            {"definition": "slang: an offensive insult", "example": "bad example"},
                        ],
                    }
                ]
            }
        ]
    )
    assert len(selected) == 1
    assert "snake" in selected[0].definition


def test_detect_query_language_uses_query_script():
    assert detect_query_language("питон") == "ru"
    assert detect_query_language("python") == "en"


def test_parse_russian_wiktionary_prefers_safe_russian_meaning():
    selected = parse_russian_wiktionary_definitions(
        """
= {{-ru-}} =
{{сущ-ru}}

==== Значение ====
# {{зоол.|ru}} крупная неядовитая [[змея]]
# {{обсц.|ru}} грубое слово
"""
    )
    assert len(selected) == 1
    assert selected[0].labels == ("зоол.",)
    assert selected[0].part_of_speech == "сущ."
    assert selected[0].definition == "крупная неядовитая змея"


def test_parse_russian_wiktionary_keeps_russian_output_when_only_obscene_meaning_exists():
    selected = parse_russian_wiktionary_definitions(
        """
= {{-ru-}} =
{{сущ-ru}}

==== Значение ====
# {{обсц.|ru}} [[женский]] [[половой орган]], [[влагалище]]
"""
    )
    assert len(selected) == 1
    assert selected[0].labels == ("обсц.",)
    assert "женский половой орган" in selected[0].definition
