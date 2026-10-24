"""サーバ上の配置先と、環境固有の設定ファイル（infra.env）の読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 形式のファイルを読む。空行と # で始まる行は無視する。"""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    etc_dir: Path
    state_dir: Path
    backup_dir: Path

    @classmethod
    def from_environ(cls) -> Paths:
        """既定の配置先を返す。テストや別配置のときは環境変数で上書きできる。"""
        env = os.environ
        return cls(
            repo_root=Path(env.get("INFRABOX_REPO_ROOT", REPO_ROOT)),
            etc_dir=Path(env.get("INFRABOX_ETC_DIR", "/etc/myinfraservicebox")),
            state_dir=Path(env.get("INFRABOX_STATE_DIR", "/var/lib/myinfraservicebox")),
            backup_dir=Path(env.get("INFRABOX_BACKUP_DIR", "/var/backups/myinfraservicebox")),
        )

    @property
    def registry_file(self) -> Path:
        return self.repo_root / "services.toml"

    @property
    def core_compose_file(self) -> Path:
        return self.repo_root / "compose.yaml"

    @property
    def infra_env_file(self) -> Path:
        return self.etc_dir / "infra.env"

    @property
    def secrets_dir(self) -> Path:
        return self.etc_dir / "secrets"

    @property
    def services_dir(self) -> Path:
        return self.state_dir / "services"

    @property
    def lock_dir(self) -> Path:
        return self.state_dir / "locks"

    @property
    def history_file(self) -> Path:
        return self.state_dir / "deploy-history.jsonl"

    @property
    def postgres_backup_dir(self) -> Path:
        return self.backup_dir / "postgres"

    def infra_env(self) -> dict[str, str]:
        try:
            return read_env_file(self.infra_env_file)
        except FileNotFoundError:
            raise SystemExit(
                f"{self.infra_env_file} がありません。先に setup/setup-server.sh を実行してください。"
            ) from None
