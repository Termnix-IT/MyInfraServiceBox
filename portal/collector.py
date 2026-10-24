"""ポータルに表示する状態（死活、デプロイ情報、リソース、コンテナ）の収集。

収集は裏で一定間隔ごとに行い、画面はその結果（スナップショット）を読むだけにする。
ページを開くたびに各サービスへ問い合わせが飛ばないようにするため。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from infrabox import history
from infrabox.registry import Registry, RegistryError, Service, load_registry

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


@dataclass(frozen=True)
class PortalSettings:
    registry_path: Path
    history_path: Path
    data_dir: Path
    host_addr: str
    docker_api: str
    refresh_seconds: float
    timezone_name: str

    @classmethod
    def from_environ(cls) -> PortalSettings:
        env = os.environ
        data_dir = Path(env.get("DATA_DIR", "/data"))
        return cls(
            registry_path=Path(env.get("REGISTRY_PATH", "/config/services.toml")),
            history_path=Path(env.get("HISTORY_PATH", str(data_dir / "deploy-history.jsonl"))),
            data_dir=data_dir,
            host_addr=env.get("HOST_ADDR", "127.0.0.1"),
            docker_api=env.get("DOCKER_API", "http://socket-proxy:2375"),
            refresh_seconds=float(env.get("REFRESH_SECONDS", "30")),
            timezone_name=env.get("PORTAL_TIMEZONE", "Asia/Tokyo"),
        )


@dataclass
class Snapshot:
    collected_at: datetime | None = None
    services: list[dict[str, Any]] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    containers: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def check_health(client: httpx.AsyncClient, service: Service, host: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        if service.health.type == "tcp":
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, service.port), timeout=3)
            writer.close()
            await writer.wait_closed()
            ok, detail = True, "TCP接続できました"
        else:
            response = await client.get(f"http://{host}:{service.port}{service.health.path}", timeout=3)
            ok = response.is_success
            detail = f"HTTP {response.status_code}"
    except (TimeoutError, OSError, httpx.HTTPError) as e:
        ok, detail = False, type(e).__name__
    return {
        "ok": ok,
        "detail": detail,
        "latency_ms": round((time.perf_counter() - start) * 1000),
    }


def container_memory(stats: dict[str, Any]) -> tuple[int | None, int | None]:
    """docker stats と同じ計算でメモリ使用量（ページキャッシュを除く）と上限を返す。"""
    memory = stats.get("memory_stats") or {}
    usage = memory.get("usage")
    if usage is None:
        return None, None
    detail = memory.get("stats") or {}
    # cgroup v2 は inactive_file、v1 は total_inactive_file
    cache = detail.get("inactive_file", detail.get("total_inactive_file", 0))
    return max(usage - cache, 0), memory.get("limit")


def collect_resources(data_dir: Path) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(str(data_dir))
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(),
        "load": [round(v, 2) for v in load],
        "memory": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "swap": {"total": swap.total, "used": swap.used, "percent": swap.percent},
        "disk": {"total": disk.total, "used": disk.used, "percent": disk.percent},
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


async def collect_containers(client: httpx.AsyncClient, docker_api: str) -> list[dict[str, Any]]:
    response = await client.get(f"{docker_api}/containers/json", params={"all": "true"}, timeout=5)
    response.raise_for_status()
    items = response.json()

    async def stats_for(item: dict[str, Any]) -> tuple[int | None, int | None]:
        if item.get("State") != "running":
            return None, None
        r = await client.get(
            f"{docker_api}/containers/{item['Id']}/stats",
            params={"stream": "false", "one-shot": "true"},
            timeout=5,
        )
        if not r.is_success:
            return None, None
        return container_memory(r.json())

    memories = await asyncio.gather(*(stats_for(item) for item in items))
    containers = []
    for item, (mem_used, mem_limit) in zip(items, memories):
        labels = item.get("Labels") or {}
        names = item.get("Names") or ["?"]
        containers.append(
            {
                "name": names[0].lstrip("/"),
                "project": labels.get(COMPOSE_PROJECT_LABEL, ""),
                "image": item.get("Image", ""),
                "state": item.get("State", ""),
                "status": item.get("Status", ""),
                "mem_used": mem_used,
                "mem_limit": mem_limit,
            }
        )
    containers.sort(key=lambda c: (c["project"], c["name"]))
    return containers


def _record_dict(record: history.DeployRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "time": datetime.fromisoformat(record.time),
        "action": record.action,
        "ref": record.ref,
        "commit": record.commit,
        "result": record.result,
        "rolled_back_to": record.rolled_back_to,
        "message": record.message,
    }


class Collector:
    def __init__(self, settings: PortalSettings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.snapshot = Snapshot()
        # 初回の cpu_percent は基準値を取るだけで 0 を返すため、起動時に一度呼んでおく
        psutil.cpu_percent(interval=None)

    async def refresh(self) -> Snapshot:
        s = self.settings
        snapshot = Snapshot(collected_at=datetime.now(UTC))

        registry: Registry | None = None
        try:
            registry = load_registry(s.registry_path)
        except RegistryError as e:
            snapshot.errors.append(str(e))

        try:
            snapshot.containers = await collect_containers(self.client, s.docker_api)
        except (httpx.HTTPError, ValueError) as e:
            snapshot.errors.append(f"コンテナの状態を取得できません: {type(e).__name__}")

        try:
            snapshot.resources = collect_resources(s.data_dir)
        except OSError as e:
            snapshot.errors.append(f"リソースを取得できません: {e}")

        if registry is not None:
            records = history.read_records(s.history_path)
            healths = await asyncio.gather(*(check_health(self.client, svc, s.host_addr) for svc in registry.services))
            for svc, health in zip(registry.services, healths):
                snapshot.services.append(
                    {
                        "name": svc.name,
                        "description": svc.description,
                        "category": svc.category,
                        "port": svc.port,
                        "external": svc.external,
                        "url": None if svc.health.type == "tcp" else f"http://{s.host_addr}:{svc.port}/",
                        "address": f"{s.host_addr}:{svc.port}",
                        "repo": svc.repo,
                        "health": health,
                        "current": _record_dict(history.last_success(records, svc.name)),
                        "last": _record_dict(history.last_record(records, svc.name)),
                        "containers": [c for c in snapshot.containers if c["project"] == svc.name],
                    }
                )
            snapshot.services.sort(key=lambda x: (x["category"], x["port"]))

        self.snapshot = snapshot
        return snapshot

    async def run_forever(self) -> None:
        while True:
            try:
                await self.refresh()
            # 収集ループが止まるとポータルが古い表示のままになるため、例外は記録して続ける
            except Exception as e:  # noqa: BLE001
                self.snapshot.errors.append(f"状態の収集に失敗しました: {type(e).__name__}")
            await asyncio.sleep(self.settings.refresh_seconds)
