from actionflow.core.llm.command_specs import COMMAND_SPECS


def test_translate_alias_parser_extracts_target_language_and_payload():
    parsed = COMMAND_SPECS["translate"].parse("JP: Hello")
    assert parsed.args["target_code"] == "JP"
    assert parsed.payload == "Hello"


def test_trans_parser_extracts_target_language_and_payload():
    parsed = COMMAND_SPECS["trans"].parse("EN: Privet")
    assert parsed.args["target_code"] == "EN"
    assert parsed.payload == "Privet"


def test_tone_parser_extracts_style_and_payload():
    parsed = COMMAND_SPECS["tone"].parse("formal: hey there")
    assert parsed.args["style"] == "formal"
    assert parsed.payload == "hey there"


def test_fill_parser_extracts_assignments_and_payload():
    parsed = COMMAND_SPECS["fill"].parse("name=Aldiyar|role=founder: Hello {{name}} from {{role}}")
    assert parsed.args == {"name": "Aldiyar", "role": "founder"}
    assert parsed.payload == "Hello {{name}} from {{role}}"
