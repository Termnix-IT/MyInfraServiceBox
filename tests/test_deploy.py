import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from infrabox import history
from infrabox.commands import CommandError
from infrabox.deploy import Deployer, DeployError
from infrabox.registry import parse_registry

COMMITS = {"main": "a" * 40, "feature": "b" * 40, "broken": "c" * 40}


def registry(database: bool = False):
    return parse_registry(
        {
            "settings": {"port_range": [8100, 8199], "reserved_ports": [80]},
            "services": [{"name": "todo", "repo": "https://example.com/todo.git", "port": 8101, "database": database}],
        }
    )


def compose_config(port: int = 8101) -> dict:
    return {
        "services": {
            "app": {"ports": [{"published": str(port)}], "networks": {"infra": None}, "mem_limit": "134217728"}
        },
        "networks": {"infra": {"name": "infra", "external": True}},
    }


class FakeServer:
    """git と docker compose の振る舞いを真似る。どの commit が起動しているかを記録する。"""

    def __init__(self, unhealthy: set[str] = frozenset(), port: int = 8101):
        self.unhealthy = unhealthy
        self.port = port
        self.checked_out: str | None = None
        self.running: str | None = None
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def run(self, args, *, env=None, input_text=None, stdin_path=None, stdout_path=None, capture=False):
        self.calls.append(args)
        if args[:2] == ["git", "clone"]:
            clone = Path(args[-1])
            (clone / ".git").mkdir(parents=True)
            (clone / "compose.yaml").write_text("services: {}\n")
            return ""
        if "rev-parse" in args:
            ref = args[-1].removesuffix("^{commit}").removeprefix("origin/")
            if ref in COMMITS:
                return COMMITS[ref] + "\n"
            if ref in COMMITS.values():
                return ref + "\n"
            raise CommandError("unknown ref")
        if "checkout" in args:
            self.checked_out = args[-1]
            return ""
        if args[:2] == ["docker", "compose"] and "config" in args:
            self.envs.append(env)
            return json.dumps(compose_config(self.port))
        if args[:2] == ["docker", "compose"] and "up" in args:
            self.running = self.checked_out
            return ""
        return ""

    def wait_healthy(self, service):
        if self.running in self.unhealthy:
            return False, "HTTP 500"
        return True, "HTTP 200"


def make_deployer(paths, server: FakeServer) -> Deployer:
    return Deployer(
        paths=paths,
        run=server.run,
        wait_healthy=server.wait_healthy,
        lock=lambda lock_dir, name: contextlib.nullcontext(),
        now=lambda: datetime(2026, 10, 25, tzinfo=UTC),
    )


def test_first_deploy_records_success(paths):
    server = FakeServer()
    record = make_deployer(paths, server).deploy(registry().get("todo"))
    assert record.succeeded
    assert record.ref == "main"
    assert record.commit == COMMITS["main"]
    assert server.running == COMMITS["main"]
    assert server.envs[0]["SERVICE_PORT"] == "8101"
    assert server.envs[0]["INFRA_NETWORK"] == "infra"
    assert history.read_records(paths.history_file) == [record]


def test_failed_health_check_rolls_back_to_previous_success(paths):
    server = FakeServer(unhealthy={COMMITS["broken"]})
    deployer = make_deployer(paths, server)
    service = registry().get("todo")
    deployer.deploy(service)
    record = deployer.deploy(service, "broken")
    assert not record.succeeded
    assert record.commit == COMMITS["broken"]
    assert record.rolled_back_to == COMMITS["main"]
    assert server.running == COMMITS["main"]
    assert "切り戻しました" in record.message


def test_failure_without_previous_version_is_left_running(paths):
    server = FakeServer(unhealthy={COMMITS["main"]})
    record = make_deployer(paths, server).deploy(registry().get("todo"))
    assert not record.succeeded
    assert record.rolled_back_to is None
    assert "以前に成功した版がない" in record.message


def test_compose_violation_stops_before_up(paths):
    server = FakeServer(port=8999)
    record = make_deployer(paths, server).deploy(registry().get("todo"))
    assert not record.succeeded
    assert "取り決め" in record.message
    assert server.running is None


def test_unknown_ref(paths):
    record = make_deployer(paths, FakeServer()).deploy(registry().get("todo"), "nope")
    assert not record.succeeded
    assert "見つかりません" in record.message


def test_ref_starting_with_dash_is_rejected(paths):
    record = make_deployer(paths, FakeServer()).deploy(registry().get("todo"), "--upload-pack=x")
    assert not record.succeeded
    assert "- で始まる" in record.message


def test_rollback_goes_to_previous_commit(paths):
    server = FakeServer()
    deployer = make_deployer(paths, server)
    service = registry().get("todo")
    deployer.deploy(service)
    deployer.deploy(service, "feature")
    record = deployer.rollback(service)
    assert record.succeeded
    assert record.action == "rollback"
    assert server.running == COMMITS["main"]


def test_rollback_without_history(paths):
    with pytest.raises(DeployError):
        make_deployer(paths, FakeServer()).rollback(registry().get("todo"))


def test_database_url_is_passed_from_secrets(paths):
    paths.secrets_dir.mkdir(parents=True)
    (paths.secrets_dir / "todo.env").write_text("DATABASE_URL=postgresql://todo:pw@db/todo\n")
    server = FakeServer()
    make_deployer(paths, server).deploy(registry(database=True).get("todo"))
    assert server.envs[0]["DATABASE_URL"] == "postgresql://todo:pw@db/todo"


def test_database_without_secrets_fails(paths):
    record = make_deployer(paths, FakeServer()).deploy(registry(database=True).get("todo"))
    assert not record.succeeded
    assert "infra db create todo" in record.message
