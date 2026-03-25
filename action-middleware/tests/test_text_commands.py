from actionflow.core.text_ops import (
    decode_base64,
    encode_base64,
    escape_text,
    fill_placeholders,
    format_reading_time,
    format_structured_text,
    redact_sensitive,
    sanitize_text,
    sha256_hex,
    sponge_case,
    text_stats,
)


def test_text_command_helpers_cover_common_transforms():
    assert sponge_case("hello world") == "hElLo WoRlD"
    assert decode_base64(encode_base64("watashi")) == "watashi"
    assert sha256_hex("abc").startswith("ba7816bf")
    assert "[EMAIL]" in redact_sensitive("mail me at test@example.com")
    assert "&lt;b&gt;x&lt;/b&gt;" == escape_text("<b>x</b>", mode="html")
    assert 'say \\"hi\\"' == escape_text('say "hi"', mode="json")
    assert "can\\'t" == escape_text("can't", mode="python")
    assert sanitize_text("**hi**\x1b[31m") == "hi"


def test_format_and_stats_helpers():
    assert '"a": 1' in format_structured_text('{"a":1}')
    stats = text_stats("one two\nthree")
    assert stats["words"] == 3
    assert stats["lines"] == 2
    assert format_reading_time(text_stats("hello")["reading_seconds"]) == "< 5 sec"
    assert format_reading_time(text_stats("word " * 120)["reading_seconds"]).startswith("~")


def test_fill_placeholders_replaces_explicit_values():
    result = fill_placeholders(
        "My name is {{name}} and I am a {{role}}",
        {"name": "Aldiyar", "role": "founder"},
    )
    assert result == "My name is Aldiyar and I am a founder"
