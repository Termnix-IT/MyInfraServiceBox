from infrabox import history
from infrabox.history import DeployRecord


def rec(commit: str, result: str = "success", service: str = "todo") -> DeployRecord:
    return DeployRecord(
        time="2026-10-25T00:00:00+00:00", service=service, action="deploy", ref="main", commit=commit, result=result
    )


def test_append_and_read_skips_broken_lines(tmp_path):
    path = tmp_path / "h.jsonl"
    history.append_record(path, rec("a"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"time": "broken"\n')
    history.append_record(path, rec("b"))
    assert [r.commit for r in history.read_records(path)] == ["a", "b"]


def test_missing_file_is_empty(tmp_path):
    assert history.read_records(tmp_path / "none.jsonl") == []


def test_last_and_previous_success():
    records = [rec("a"), rec("x", service="other"), rec("b"), rec("c", result="failed"), rec("b")]
    assert history.last_success(records, "todo").commit == "b"
    assert history.previous_success(records, "todo").commit == "a"
    assert history.last_record(records, "todo").commit == "b"
    assert history.previous_success(records, "other") is None
