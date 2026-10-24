"""サービスのデプロイと切り戻し。

処理の流れ: ロック → repo の fetch と checkout → compose の検査 → build と起動
→ ヘルスチェック → 失敗したら直前に成功した版へ戻す → 履歴に記録。
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import history
from .commands import CommandError, Runner
from .compose_check import check_compose_config
from .history import DeployRecord
from .registry import Service
from .settings import Paths, read_env_file

HealthWaiter = Callable[[Service], tuple[bool, str]]


class DeployError(Exception):
    pass


class LockBusyError(DeployError):
    pass


@contextlib.contextmanager
def service_lock(lock_dir: Path, name: str) -> Iterator[None]:
    """同じサービスへのデプロイが同時に走らないようにする（Linux の flock を使う）。"""
    import fcntl

    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{name}.lock").open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusyError(f"{name} は別のデプロイが実行中です") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@dataclass
class Deployer:
    paths: Paths
    run: Runner
    wait_healthy: HealthWaiter
    network_name: str = "infra"
    lock: Callable[[Path, str], contextlib.AbstractContextManager[None]] = service_lock
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def deploy(self, service: Service, ref: str | None = None) -> DeployRecord:
        ref = ref or service.branch
        with self.lock(self.paths.lock_dir, service.name):
            records = history.read_records(self.paths.history_file)
            fallback = history.last_success(records, service.name)
            return self._release(service, ref, "deploy", fallback)

    def rollback(self, service: Service) -> DeployRecord:
        with self.lock(self.paths.lock_dir, service.name):
            records = history.read_records(self.paths.history_file)
            target = history.previous_success(records, service.name)
            if target is None or target.commit is None:
                raise DeployError(f"{service.name} には戻せる以前の成功した版がありません")
            # 切り戻し自体が失敗したときは、いま動いている版をもう一度起動して戻す
            return self._release(service, target.commit, "rollback", history.last_success(records, service.name))

    def _release(self, service: Service, ref: str, action: str, fallback: DeployRecord | None) -> DeployRecord:
        commit: str | None = None
        try:
            commit = self._checkout(service, ref)
            self._compose_up(service)
            ok, detail = self.wait_healthy(service)
            if not ok:
                raise DeployError(f"ヘルスチェックに失敗しました: {detail}")
        except (CommandError, DeployError) as e:
            message = str(e)
            rolled_back_to = None
            if fallback is None or not fallback.commit:
                message += " / 以前に成功した版がないため、切り戻しはしていません"
            elif fallback.commit == commit:
                message += " / 直前に成功した版と同じcommitのため、切り戻しはしていません"
            else:
                rolled_back_to, note = self._restore(service, fallback.commit)
                message += f" / {note}"
            return self._record(service, action, ref, commit, history.RESULT_FAILED, rolled_back_to, message)
        return self._record(service, action, ref, commit, history.RESULT_SUCCESS, None, detail)

    def _restore(self, service: Service, commit: str) -> tuple[str | None, str]:
        try:
            self._checkout(service, commit)
            self._compose_up(service)
            ok, detail = self.wait_healthy(service)
        except (CommandError, DeployError) as e:
            return None, f"{commit[:12]} への切り戻しにも失敗しました: {e}"
        if not ok:
            return None, f"{commit[:12]} へ切り戻しましたが、ヘルスチェックに失敗しました: {detail}"
        return commit, f"{commit[:12]} へ切り戻しました"

    def _record(
        self,
        service: Service,
        action: str,
        ref: str,
        commit: str | None,
        result: str,
        rolled_back_to: str | None,
        message: str,
    ) -> DeployRecord:
        record = DeployRecord(
            time=self.now().isoformat(timespec="seconds"),
            service=service.name,
            action=action,
            ref=ref,
            commit=commit,
            result=result,
            rolled_back_to=rolled_back_to,
            message=message,
        )
        history.append_record(self.paths.history_file, record)
        return record

    def _clone_dir(self, service: Service) -> Path:
        return self.paths.services_dir / service.name

    def _checkout(self, service: Service, ref: str) -> str:
        if ref.startswith("-"):
            raise DeployError(f"ref に - で始まる値は指定できません: {ref}")
        clone = self._clone_dir(service)
        assert service.repo is not None
        if not (clone / ".git").exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "clone", "--quiet", service.repo, str(clone)])
        else:
            self.run(["git", "-C", str(clone), "remote", "set-url", "origin", service.repo])
        self.run(["git", "-C", str(clone), "fetch", "--quiet", "--prune", "--tags", "origin"])

        commit = None
        for candidate in (f"origin/{ref}", ref):
            try:
                out = self.run(
                    ["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                    capture=True,
                )
            except CommandError:
                continue
            commit = out.strip()
            break
        if not commit:
            raise DeployError(f"{service.name} の repo に ref {ref} が見つかりません")

        self.run(
            [
                "git",
                "-C",
                str(clone),
                "-c",
                "advice.detachedHead=false",
                "checkout",
                "--quiet",
                "--force",
                "--detach",
                commit,
            ]
        )
        return commit

    def _compose_env(self, service: Service) -> dict[str, str]:
        env = os.environ.copy()
        env["SERVICE_PORT"] = str(service.port)
        env["INFRA_NETWORK"] = self.network_name
        if service.database:
            secret_file = self.paths.secrets_dir / f"{service.name}.env"
            try:
                url = read_env_file(secret_file).get("DATABASE_URL")
            except FileNotFoundError:
                url = None
            if not url:
                raise DeployError(f"{secret_file} がありません。先に infra db create {service.name} を実行してください")
            env["DATABASE_URL"] = url
        return env

    def _compose_up(self, service: Service) -> None:
        clone = self._clone_dir(service)
        compose_file = clone / "compose.yaml"
        if not compose_file.exists():
            raise DeployError(f"{service.name} の repo に compose.yaml がありません")
        env = self._compose_env(service)
        base = [
            "docker", "compose",
            "--project-name", service.name,
            "--project-directory", str(clone),
            "--file", str(compose_file),
        ]  # fmt: skip
        config = json.loads(self.run([*base, "config", "--format", "json"], env=env, capture=True))
        problems = check_compose_config(config, service, self.network_name)
        if problems:
            raise DeployError("compose.yaml が取り決めを守っていません: " + "; ".join(problems))
        self.run([*base, "up", "--detach", "--build", "--remove-orphans"], env=env)
