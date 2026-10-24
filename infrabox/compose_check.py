"""サービスrepoの compose.yaml が基盤との取り決めを守っているかの検査。

`docker compose config --format json` の出力（変数展開済みの構成）を対象にする。
"""

from __future__ import annotations

from typing import Any

from .registry import Service, parse_memory


def check_compose_config(config: dict[str, Any], service: Service, network_name: str) -> list[str]:
    problems: list[str] = []

    network_keys = {
        key
        for key, network in (config.get("networks") or {}).items()
        if isinstance(network, dict) and network.get("external") and network.get("name") == network_name
    }
    if not network_keys:
        problems.append(f"networks に external: true の {network_name} ネットワークがありません")

    services = config.get("services") or {}
    if not services:
        problems.append("services が空です")

    published_found = False
    attached = False
    total_memory = 0
    for name, svc in services.items():
        for port in svc.get("ports") or []:
            published = port.get("published")
            if published in (None, ""):
                problems.append(f"{name}: 公開ポートの番号が指定されていません（番号を固定してください）")
                continue
            if str(published) != str(service.port):
                problems.append(f"{name}: 公開ポート {published} が台帳の port {service.port} と違います")
            else:
                published_found = True

        if network_keys & set(svc.get("networks") or {}):
            attached = True

        deploy_limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        limit = svc.get("mem_limit") or deploy_limits.get("memory")
        if limit in (None, "", 0, "0"):
            problems.append(f"{name}: メモリ上限（mem_limit）が指定されていません")
            continue
        try:
            total_memory += parse_memory(limit)
        except ValueError as e:
            problems.append(f"{name}: {e}")

    if services and not published_found:
        problems.append(f"台帳の port {service.port} を公開しているサービスがありません")
    if services and network_keys and not attached:
        problems.append(f"{network_name} ネットワークに参加しているサービスがありません")
    if total_memory > service.memory_bytes:
        problems.append(
            f"メモリ上限の合計 {total_memory // 1024**2}MiB が台帳の memory {service.memory} を超えています"
        )
    return problems
