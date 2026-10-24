import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from infrabox import history
from infrabox.history import DeployRecord
from portal.collector import Collector, PortalSettings, container_memory
from portal.main import create_app, format_bytes

REGISTRY = """
[settings]
port_range = [8100, 8199]
reserved_ports = [80, 25565]

[[services]]
name = "todo"
description = "ToDo"
category = "検証"
repo = "https://example.com/todo.git"
port = 8101

[[services]]
name = "minecraft"
port = 25565
external = true
"""

CONTAINERS = [
    {
        "Id": "f" * 64,
        "Names": ["/todo-app-1"],
        "Image": "todo-app",
        "State": "running",
        "Status": "Up 3 hours",
        "Labels": {"com.docker.compose.project": "todo"},
    },
    {"Id": "e" * 64, "Names": ["/old"], "Image": "old", "State": "exited", "Status": "Exited (0)", "Labels": {}},
]
STATS = {"memory_stats": {"usage": 60 * 1024**2, "limit": 128 * 1024**2, "stats": {"inactive_file": 10 * 1024**2}}}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "socket-proxy":
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=CONTAINERS)
        return httpx.Response(200, json=STATS)
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(404)


def settings(tmp_path: Path) -> PortalSettings:
    registry = tmp_path / "services.toml"
    registry.write_text(REGISTRY, encoding="utf-8")
    return PortalSettings(
        registry_path=registry,
        history_path=tmp_path / "deploy-history.jsonl",
        data_dir=tmp_path,
        host_addr="127.0.0.1",
        docker_api="http://socket-proxy:2375",
        refresh_seconds=30,
        timezone_name="Asia/Tokyo",
    )


def collect(s: PortalSettings):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Collector(s, client).refresh()

    return asyncio.run(go())


def test_container_memory_excludes_page_cache():
    assert container_memory(STATS) == (50 * 1024**2, 128 * 1024**2)
    assert container_memory({}) == (None, None)


def test_collect_snapshot(tmp_path):
    s = settings(tmp_path)
    history.append_record(
        s.history_path,
        DeployRecord("2026-10-25T00:00:00+00:00", "todo", "deploy", "main", "a" * 40, "success", None, "ok"),
    )
    snapshot = collect(s)
    todo = next(x for x in snapshot.services if x["name"] == "todo")
    minecraft = next(x for x in snapshot.services if x["name"] == "minecraft")
    assert todo["health"]["ok"]
    assert todo["url"] == "http://127.0.0.1:8101/"
    assert todo["current"]["commit"] == "a" * 40
    assert todo["containers"][0]["mem_used"] == 50 * 1024**2
    # Minecraft はこのテスト環境では起動していないので、TCP接続できず停止中になる
    assert minecraft["url"] is None
    assert not minecraft["health"]["ok"]
    assert snapshot.resources["memory"]["total"] > 0
    assert len(snapshot.containers) == 2


def test_broken_registry_is_shown_as_error(tmp_path):
    s = settings(tmp_path)
    s.registry_path.write_text("[[services]]\nname = 1\n", encoding="utf-8")
    snapshot = collect(s)
    assert snapshot.services == []
    assert any("services.toml" in e for e in snapshot.errors)


def test_pages_render(tmp_path):
    app = create_app(settings(tmp_path), start_collector=False)
    with TestClient(app) as client:
        assert "状態を収集しています" in client.get("/").text
        app.state.collector.snapshot = collect(settings(tmp_path))
        page = client.get("/partials/status").text
        assert "todo" in page and "minecraft" in page and "50.0 MiB" in page
        data = client.get("/api/status").json()
        assert json.dumps(data)
        assert client.get("/healthz").json() == {"status": "ok"}


def test_format_bytes():
    assert format_bytes(None) == "-"
    assert format_bytes(512) == "512 B"
    assert format_bytes(3 * 1024**3) == "3.0 GiB"
