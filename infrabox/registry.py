"""サービス台帳（services.toml）の読み込みと検査。

デプロイスクリプトとポータルの両方がこのモジュールで台帳を読み、解釈がずれないようにする。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
DEFAULT_MEMORY = "256m"
HEALTH_TYPES = ("http", "tcp")

_SIZE_PATTERN = re.compile(r"^(\d+)\s*([bkmg]?)b?$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

_SETTINGS_KEYS = {"port_range", "reserved_ports"}
_SERVICE_KEYS = {
    "name",
    "description",
    "category",
    "port",
    "health",
    "external",
    "repo",
    "branch",
    "database",
    "memory",
    "memory_reason",
}
_HEALTH_KEYS = {"type", "path"}


class RegistryError(Exception):
    """台帳に1件以上の問題があるときに送出する。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("services.toml に問題があります:\n" + "\n".join(f"- {p}" for p in problems))


def parse_memory(value: str | int) -> int:
    """'256m' や '134217728' のようなメモリ量をバイト数に変換する。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        # 呼び出し側は台帳の書き間違いとして ValueError だけを扱うため、型の違いも ValueError にする
        raise ValueError(f"メモリ量として解釈できません: {value!r}")  # noqa: TRY004
    match = _SIZE_PATTERN.match(value.strip())
    if not match:
        raise ValueError(f"メモリ量として解釈できません: {value!r}")
    return int(match.group(1)) * _SIZE_UNITS[match.group(2).lower()]


@dataclass(frozen=True)
class HealthCheck:
    type: str
    path: str | None = None


@dataclass(frozen=True)
class Service:
    name: str
    description: str
    category: str
    port: int
    health: HealthCheck
    external: bool = False
    repo: str | None = None
    branch: str = "main"
    database: bool = False
    memory: str = DEFAULT_MEMORY
    memory_reason: str | None = None

    @property
    def memory_bytes(self) -> int:
        return parse_memory(self.memory)


@dataclass(frozen=True)
class Registry:
    port_range: tuple[int, int]
    reserved_ports: frozenset[int]
    services: tuple[Service, ...]

    def get(self, name: str) -> Service:
        for service in self.services:
            if service.name == name:
                return service
        raise KeyError(name)

    @property
    def managed(self) -> tuple[Service, ...]:
        """基盤がデプロイするサービス（基盤の外で動くものを除く）。"""
        return tuple(s for s in self.services if not s.external)


def load_registry(path: Path) -> Registry:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise RegistryError([f"{path} が見つかりません"]) from None
    except tomllib.TOMLDecodeError as e:
        raise RegistryError([f"TOMLとして読めません: {e}"]) from None
    return parse_registry(data)


def parse_registry(data: dict[str, Any]) -> Registry:
    problems: list[str] = []

    settings = data.get("settings", {})
    for key in sorted(set(settings) - _SETTINGS_KEYS):
        problems.append(f"[settings] に未知の項目 {key} があります")
    port_range = _parse_port_range(settings.get("port_range"), problems)
    reserved_ports = _parse_reserved_ports(settings.get("reserved_ports", []), problems)

    for key in sorted(set(data) - {"settings", "services"}):
        problems.append(f"未知の項目 {key} があります")

    raw_services = data.get("services", [])
    if not isinstance(raw_services, list):
        problems.append("services は [[services]] の配列で書いてください")
        raw_services = []

    services: list[Service] = []
    for index, raw in enumerate(raw_services, start=1):
        service = _parse_service(raw, index, port_range, reserved_ports, problems)
        if service is not None:
            services.append(service)

    _check_duplicates(services, problems)

    if problems:
        raise RegistryError(problems)
    return Registry(port_range=port_range, reserved_ports=reserved_ports, services=tuple(services))


def _parse_port_range(value: Any, problems: list[str]) -> tuple[int, int]:
    if isinstance(value, list) and len(value) == 2 and all(_is_port(v) for v in value) and value[0] <= value[1]:
        return (value[0], value[1])
    problems.append("[settings] port_range は [開始, 終了] のポート番号で書いてください")
    return (0, -1)


def _parse_reserved_ports(value: Any, problems: list[str]) -> frozenset[int]:
    if isinstance(value, list) and all(_is_port(v) for v in value):
        return frozenset(value)
    problems.append("[settings] reserved_ports はポート番号の配列で書いてください")
    return frozenset()


def _parse_service(
    raw: Any,
    index: int,
    port_range: tuple[int, int],
    reserved_ports: frozenset[int],
    problems: list[str],
) -> Service | None:
    if not isinstance(raw, dict):
        problems.append(f"{index}件目のサービスがテーブルではありません")
        return None

    name = raw.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        problems.append(f"{index}件目: name は英小文字で始まる英小文字・数字・_ の2〜31文字にしてください（{name!r}）")
        return None
    label = f"サービス {name}"
    start = len(problems)

    for key in sorted(set(raw) - _SERVICE_KEYS):
        problems.append(f"{label}: 未知の項目 {key} があります")

    external = raw.get("external", False)
    if not isinstance(external, bool):
        problems.append(f"{label}: external は true か false で書いてください")
        external = False

    port = raw.get("port")
    if not _is_port(port):
        problems.append(f"{label}: port はポート番号で書いてください")
        port = 0
    elif not external:
        if port in reserved_ports:
            problems.append(f"{label}: port {port} は予約ポートです")
        elif not port_range[0] <= port <= port_range[1]:
            problems.append(f"{label}: port {port} が割り当て範囲 {port_range[0]}〜{port_range[1]} の外です")

    repo = raw.get("repo")
    if external:
        if repo is not None:
            problems.append(f"{label}: 基盤の外で動くサービス（external = true）には repo を書けません")
    elif not isinstance(repo, str) or not repo:
        problems.append(f"{label}: repo にサービスrepoのURLを書いてください")

    health = _parse_health(raw.get("health"), external, label, problems)

    memory = raw.get("memory", DEFAULT_MEMORY)
    memory_reason = raw.get("memory_reason")
    try:
        memory_bytes = parse_memory(memory)
    except ValueError as e:
        problems.append(f"{label}: memory {e}")
    else:
        if memory_bytes > parse_memory(DEFAULT_MEMORY) and not memory_reason:
            problems.append(
                f"{label}: memory を {DEFAULT_MEMORY} より大きくする場合は memory_reason に理由を書いてください"
            )

    for key in ("description", "category", "branch", "memory_reason"):
        if key in raw and not isinstance(raw[key], str):
            problems.append(f"{label}: {key} は文字列で書いてください")
    database = raw.get("database", False)
    if not isinstance(database, bool):
        problems.append(f"{label}: database は true か false で書いてください")
    elif database and external:
        problems.append(f"{label}: 基盤の外で動くサービスには database を指定できません")

    if len(problems) > start:
        return None
    return Service(
        name=name,
        description=raw.get("description", ""),
        category=raw.get("category", "その他"),
        port=port,
        health=health,
        external=external,
        repo=repo,
        branch=raw.get("branch", "main"),
        database=database,
        memory=str(memory),
        memory_reason=memory_reason,
    )


def _parse_health(raw: Any, external: bool, label: str, problems: list[str]) -> HealthCheck:
    if raw is None:
        return HealthCheck(type="tcp") if external else HealthCheck(type="http", path="/health")
    if not isinstance(raw, dict):
        problems.append(f"{label}: health はテーブルで書いてください")
        return HealthCheck(type="tcp")
    for key in sorted(set(raw) - _HEALTH_KEYS):
        problems.append(f"{label}: health に未知の項目 {key} があります")
    kind = raw.get("type", "http")
    if kind not in HEALTH_TYPES:
        problems.append(f"{label}: health.type は http か tcp にしてください")
        return HealthCheck(type="tcp")
    if kind == "tcp":
        return HealthCheck(type="tcp")
    path = raw.get("path", "/health")
    if not isinstance(path, str) or not path.startswith("/"):
        problems.append(f"{label}: health.path は / で始まるパスにしてください")
        path = "/health"
    return HealthCheck(type="http", path=path)


def _check_duplicates(services: list[Service], problems: list[str]) -> None:
    seen_names: set[str] = set()
    seen_ports: dict[int, str] = {}
    for service in services:
        if service.name in seen_names:
            problems.append(f"サービス名 {service.name} が重複しています")
        seen_names.add(service.name)
        if service.port in seen_ports:
            problems.append(f"port {service.port} が {seen_ports[service.port]} と {service.name} で重複しています")
        else:
            seen_ports[service.port] = service.name


def _is_port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
