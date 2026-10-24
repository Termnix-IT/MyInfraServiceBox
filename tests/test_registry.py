from pathlib import Path

import pytest

from infrabox.registry import RegistryError, load_registry, parse_memory, parse_registry

ROOT = Path(__file__).resolve().parents[1]


def make(services: list[dict], **settings) -> dict:
    base = {"port_range": [8100, 8199], "reserved_ports": [22, 80, 25565]}
    base.update(settings)
    return {"settings": base, "services": services}


def svc(**overrides) -> dict:
    data = {"name": "todo", "repo": "https://example.com/todo.git", "port": 8101}
    data.update(overrides)
    return data


def problems_of(data: dict) -> list[str]:
    with pytest.raises(RegistryError) as e:
        parse_registry(data)
    return e.value.problems


def test_repo_registry_is_valid():
    registry = load_registry(ROOT / "services.toml")
    minecraft = registry.get("minecraft")
    assert minecraft.external
    assert minecraft.health.type == "tcp"
    assert registry.managed == ()


def test_defaults_for_managed_service():
    service = parse_registry(make([svc()])).get("todo")
    assert service.health.type == "http"
    assert service.health.path == "/health"
    assert service.branch == "main"
    assert service.memory_bytes == 256 * 1024**2
    assert not service.database


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"port": 80}, "予約ポート"),
        ({"port": 9000}, "割り当て範囲"),
        ({"name": "Todo"}, "name は"),
        ({"name": "x"}, "name は"),
        ({"repo": None}, "repo"),
        ({"memory": "512m"}, "memory_reason"),
        ({"memory": "lots"}, "メモリ量"),
        ({"health": {"type": "http", "path": "health"}}, "health.path"),
        ({"health": {"type": "grpc"}}, "health.type"),
        ({"databse": True}, "未知の項目 databse"),
    ],
)
def test_invalid_service(overrides, expected):
    raw = svc(**overrides)
    raw = {k: v for k, v in raw.items() if v is not None}
    problems = problems_of(make([raw]))
    assert any(expected in p for p in problems), problems


def test_memory_over_default_with_reason_is_allowed():
    service = parse_registry(make([svc(memory="512m", memory_reason="画像処理で使う")])).get("todo")
    assert service.memory_bytes == 512 * 1024**2


def test_duplicate_ports_and_names():
    problems = problems_of(make([svc(), svc(name="memo"), svc(port=8102)]))
    assert any("8101" in p and "重複" in p for p in problems)
    assert any("サービス名 todo" in p for p in problems)


def test_external_service_may_use_reserved_port_but_not_repo():
    ok = parse_registry(make([{"name": "minecraft", "port": 25565, "external": True}]))
    assert ok.get("minecraft").health.type == "tcp"
    problems = problems_of(make([{"name": "game", "port": 25565, "external": True, "repo": "x"}]))
    assert any("repo を書けません" in p for p in problems)


def test_external_port_must_not_collide_with_managed_service():
    problems = problems_of(make([svc(), {"name": "other", "port": 8101, "external": True}]))
    assert any("重複" in p for p in problems)


def test_broken_settings_are_reported():
    problems = problems_of(make([svc()], port_range=[8199, 8100]))
    assert any("port_range" in p for p in problems)


def test_toml_syntax_error(tmp_path):
    path = tmp_path / "services.toml"
    path.write_text("[[services]\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("256m", 256 * 1024**2), ("1g", 1024**3), ("134217728", 134217728), (1024, 1024), ("64MB", 64 * 1024**2)],
)
def test_parse_memory(value, expected):
    assert parse_memory(value) == expected
