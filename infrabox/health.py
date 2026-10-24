"""デプロイ時のヘルスチェック（同期版）。ポータルは同じ判定を非同期で行う。"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request

from .registry import Service


def check_once(service: Service, host: str, timeout: float = 3.0) -> tuple[bool, str]:
    if service.health.type == "tcp":
        try:
            with socket.create_connection((host, service.port), timeout=timeout):
                return True, "TCP接続できました"
        except OSError as e:
            return False, f"TCP接続できません: {e}"

    url = f"http://{host}:{service.port}{service.health.path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return True, f"{url} が {response.status} を返しました"
            return False, f"{url} が {response.status} を返しました"
    except urllib.error.HTTPError as e:
        return False, f"{url} が {e.code} を返しました"
    except (urllib.error.URLError, OSError) as e:
        return False, f"{url} に接続できません: {e}"


def wait_healthy(
    service: Service,
    host: str = "127.0.0.1",
    timeout: float = 60.0,
    interval: float = 2.0,
) -> tuple[bool, str]:
    """timeout 秒以内にヘルスチェックが成功すれば True を返す。"""
    deadline = time.monotonic() + timeout
    while True:
        ok, detail = check_once(service, host)
        if ok or time.monotonic() >= deadline:
            return ok, detail
        time.sleep(interval)
