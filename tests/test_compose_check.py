from infrabox.compose_check import check_compose_config
from infrabox.registry import parse_registry

SERVICE = parse_registry(
    {
        "settings": {"port_range": [8100, 8199], "reserved_ports": [80]},
        "services": [{"name": "todo", "repo": "https://example.com/todo.git", "port": 8101}],
    }
).get("todo")


def config(**app_overrides) -> dict:
    app = {
        "ports": [{"mode": "ingress", "target": 8080, "published": "8101", "protocol": "tcp"}],
        "networks": {"infra": None},
        "mem_limit": "134217728",
    }
    app.update(app_overrides)
    return {
        "name": "todo",
        "services": {"app": app},
        "networks": {"infra": {"name": "infra", "external": True}},
    }


def test_valid_config():
    assert check_compose_config(config(), SERVICE, "infra") == []


def test_wrong_published_port():
    problems = check_compose_config(config(ports=[{"target": 8080, "published": "8102"}]), SERVICE, "infra")
    assert any("8102" in p for p in problems)
    assert any("公開しているサービスがありません" in p for p in problems)


def test_random_host_port_is_rejected():
    problems = check_compose_config(config(ports=[{"target": 8080}]), SERVICE, "infra")
    assert any("番号が指定されていません" in p for p in problems)


def test_missing_memory_limit():
    cfg = config()
    del cfg["services"]["app"]["mem_limit"]
    problems = check_compose_config(cfg, SERVICE, "infra")
    assert any("mem_limit" in p for p in problems)


def test_deploy_resources_memory_is_accepted():
    cfg = config()
    del cfg["services"]["app"]["mem_limit"]
    cfg["services"]["app"]["deploy"] = {"resources": {"limits": {"memory": "134217728"}}}
    assert check_compose_config(cfg, SERVICE, "infra") == []


def test_total_memory_over_registry_limit():
    cfg = config()
    cfg["services"]["worker"] = {"mem_limit": "201326592", "networks": {"infra": None}}
    problems = check_compose_config(cfg, SERVICE, "infra")
    assert any("超えています" in p for p in problems)


def test_infra_network_required():
    cfg = config()
    cfg["networks"] = {"default": {"name": "todo_default"}}
    problems = check_compose_config(cfg, SERVICE, "infra")
    assert any("external: true の infra" in p for p in problems)


def test_service_must_join_infra_network():
    problems = check_compose_config(config(networks={"default": None}), SERVICE, "infra")
    assert any("参加しているサービスがありません" in p for p in problems)
