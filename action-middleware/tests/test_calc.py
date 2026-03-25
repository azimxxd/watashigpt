from actionflow.core.text_ops import safe_eval_math


def test_safe_eval_math_supports_percent_and_functions():
    assert safe_eval_math("15% of 340") == "51"
    assert safe_eval_math("sqrt(144)") == "12"
    assert safe_eval_math("__import__('os').system('whoami')") is None
