from datetime import datetime

import pytest

from infrabox import database


class FakeDocker:
    def __init__(self, databases=("memo", "todo")):
        self.databases = databases
        self.calls = []
        self.inputs = []

    def run(self, args, *, env=None, input_text=None, stdin_path=None, stdout_path=None, capture=False):
        self.calls.append(args)
        if input_text is not None:
            self.inputs.append(input_text)
        if "-At" in args:
            return "\n".join(self.databases) + "\n"
        if stdout_path is not None:
            stdout_path.write_text(" ".join(args))
        return ""


def test_create_database_writes_secret_and_keeps_password_off_argv(paths):
    docker = FakeDocker()
    secret, created = database.create_database(paths, docker.run, "todo")
    assert created
    url = secret.read_text().strip().removeprefix("DATABASE_URL=")
    password = url.split(":")[2].split("@")[0]
    assert url.startswith("postgresql://todo:") and url.endswith("@infrabox-postgres:5432/todo")
    assert all(password not in " ".join(call) for call in docker.calls)
    assert f"\\set pw '{password}'" in docker.inputs[0]

    _, created_again = database.create_database(paths, docker.run, "todo")
    assert not created_again
    assert len(docker.calls) == 1


def test_create_database_rejects_unsafe_name(paths):
    with pytest.raises(ValueError):
        database.create_database(paths, FakeDocker().run, "todo'; DROP")


def test_backup_and_prune(paths):
    docker = FakeDocker()
    for day in range(1, 10):
        database.backup(paths, docker.run, datetime(2026, 10, day, 4, 30), keep=7)
    stamps = sorted(p.name for p in paths.postgres_backup_dir.iterdir())
    assert stamps == [f"202610{day:02d}-043000" for day in range(3, 10)]
    latest = paths.postgres_backup_dir / stamps[-1]
    assert sorted(p.name for p in latest.iterdir()) == ["globals.sql", "memo.dump", "todo.dump"]


def test_restore_validates_input(paths):
    docker = FakeDocker()
    with pytest.raises(ValueError):
        database.restore(paths, docker.run, "latest", "todo")
    with pytest.raises(FileNotFoundError):
        database.restore(paths, docker.run, "20261025-043000", "todo")
    dump = paths.postgres_backup_dir / "20261025-043000" / "todo.dump"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"x")
    assert database.restore(paths, docker.run, "20261025-043000", "todo") == dump
    assert docker.calls[-1][-1] == "todo"
