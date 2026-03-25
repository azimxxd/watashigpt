from actionflow.app import runtime


def test_explicit_command_prefix_bypasses_picker():
    commands = {
        "rewrite": {"prefixes": ["RW:", "REWRITE:"]},
        "trans": {"prefixes": ["TRANS:"]},
    }
    assert runtime._has_explicit_prefix_command("RW: привет", commands) is True
    assert runtime._has_explicit_prefix_command("TRANS:JA: Доброе утро", commands) is True
    assert runtime._has_explicit_prefix_command("обычный текст без команды", commands) is False
