import json

from actionflow.core.history import PatternLearner, load_history_entries


def test_pattern_learner_scores_context(tmp_path):
    history_path = tmp_path / "history.jsonl"
    with history_path.open("w", encoding="utf-8") as handle:
        for _ in range(24):
            handle.write(json.dumps({"command": "rewrite", "app_context": "docs"}) + "\n")
        for _ in range(6):
            handle.write(json.dumps({"command": "count", "app_context": "docs"}) + "\n")

    learner = PatternLearner(history_path)
    learner.load()

    scores = learner.get_scores("docs")
    assert learner.sample_count == 30
    assert scores["rewrite"] > scores["count"]


def test_history_loader_reads_utf8_jsonl(tmp_path):
    history_path = tmp_path / "history.jsonl"
    payload = {"command": "define", "input": "питон", "output": "змея"}
    history_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    entries = load_history_entries(history_path, limit=None)

    assert entries[0]["input"] == "питон"
    assert entries[0]["output"] == "змея"


def test_history_loader_recovers_cp1251_file_without_crash(tmp_path):
    history_path = tmp_path / "history.jsonl"
    payload = {"command": "define", "input": "питон", "output": "змея"}
    history_path.write_bytes((json.dumps(payload, ensure_ascii=False) + "\n").encode("cp1251"))

    entries = load_history_entries(history_path, limit=None)

    assert entries[0]["input"] == "питон"
    assert "змея" in history_path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("history.bak_*"))


def test_history_loader_recovers_corrupted_bytes_without_crash(tmp_path):
    history_path = tmp_path / "history.jsonl"
    history_path.write_bytes(b'{"command":"count","input":"ok","output":"ok"}\n\xff\xfe\xfa\n')

    entries = load_history_entries(history_path, limit=None)

    assert entries[0]["command"] == "count"
    assert list(tmp_path.glob("history.bak_*"))
