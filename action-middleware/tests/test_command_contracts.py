from actionflow.core.command_contracts import COMMAND_CONTRACTS


def test_target_commands_have_product_contracts():
    required = {
        "count",
        "escape",
        "clip",
        "stack",
        "command",
        "wiki",
        "define",
        "explain",
        "email",
        "haiku",
        "fill",
    }
    assert required.issubset(COMMAND_CONTRACTS.keys())


def test_each_contract_has_validation_rules_and_error_behavior():
    for name, contract in COMMAND_CONTRACTS.items():
        assert contract.purpose
        assert contract.expected_input
        assert contract.expected_output_schema
        assert contract.language_behavior
        assert contract.validation_rules
        assert contract.error_behavior
