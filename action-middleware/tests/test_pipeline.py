from actionflow.core.pipeline import execute_pipeline, run_command_chain


def test_pipeline_parses_and_executes_chain():
    commands = {
        "translate": {"prefixes": ["TR:"]},
        "summarize": {"prefixes": ["SUM:"]},
    }
    chain = run_command_chain("TR:|SUM: hello world", commands)
    assert chain is not None
    steps, payload = chain
    assert payload == " hello world"

    def executor(name: str, text: str, _config: dict) -> str:
        if name == "translate":
            return text.upper()
        if name == "summarize":
            return text[:5]
        return text

    assert execute_pipeline(payload.strip(), steps, executor) == "HELLO"
