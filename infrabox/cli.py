"""`infra` コマンドの入口。サーバ上で bin/infra から呼び出す。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from . import database, history
from .commands import CommandError, run
from .deploy import Deployer, DeployError
from .health import wait_healthy
from .registry import Registry, RegistryError, Service, load_registry
from .settings import Paths

CORE_PROJECT = "infrabox"


def main(argv: list[str] | None = None) -> int:
    # 履歴やclone先をグループ（infrabox）で共有するため、グループに書き込み権限を残す
    os.umask(0o002)
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = Paths.from_environ()
    try:
        return args.handler(args, paths)
    except RegistryError as e:
        print(e, file=sys.stderr)
    except (CommandError, DeployError, ValueError, FileNotFoundError) as e:
        print(f"エラー: {e}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infra", description="MyInfraServiceBox の基盤操作")
    sub = parser.add_subparsers(required=True, metavar="<command>")

    p = sub.add_parser("check", help="services.toml を検査する")
    p.set_defaults(handler=cmd_check)

    p = sub.add_parser("list", help="サービスの一覧と動いている版を表示する")
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("deploy", help="サービスをデプロイする")
    p.add_argument("service")
    p.add_argument("--ref", help="デプロイするブランチ・タグ・commit（既定は台帳の branch）")
    p.add_argument("--timeout", type=float, default=60.0, help="ヘルスチェックを待つ秒数")
    p.set_defaults(handler=cmd_deploy)

    p = sub.add_parser("rollback", help="いま動いている版の1つ前に動いていた版へ戻す")
    p.add_argument("service")
    p.add_argument("--timeout", type=float, default=60.0, help="ヘルスチェックを待つ秒数")
    p.set_defaults(handler=cmd_rollback)

    p = sub.add_parser("history", help="デプロイ履歴を表示する")
    p.add_argument("service", nargs="?")
    p.add_argument("-n", type=int, default=20, help="表示する件数")
    p.set_defaults(handler=cmd_history)

    p = sub.add_parser("core", help="基盤本体（PostgreSQL・ポータル）を操作する")
    p.add_argument("action", choices=["up", "down", "ps"])
    p.set_defaults(handler=cmd_core)

    p = sub.add_parser("db", help="共有PostgreSQLにサービス用のDBを作る")
    p.add_argument("action", choices=["create"])
    p.add_argument("service")
    p.set_defaults(handler=cmd_db)

    p = sub.add_parser("backup", help="共有PostgreSQLをバックアップする")
    p.add_argument("--keep", type=int, default=7, help="残す世代数")
    p.set_defaults(handler=cmd_backup)

    p = sub.add_parser("restore", help="バックアップから1つのDBを復元する")
    p.add_argument("stamp", help="バックアップ名（YYYYmmdd-HHMMSS）")
    p.add_argument("database")
    p.add_argument("--yes", action="store_true", help="確認なしで上書きする")
    p.set_defaults(handler=cmd_restore)
    return parser


def _registry(paths: Paths) -> Registry:
    return load_registry(paths.registry_file)


def _managed_service(registry: Registry, name: str) -> Service:
    try:
        service = registry.get(name)
    except KeyError:
        raise ValueError(f"{name} は services.toml に登録されていません") from None
    if service.external:
        raise ValueError(f"{name} は基盤の外で動くサービスなので、infra では操作できません")
    return service


def _deployer(paths: Paths, timeout: float) -> Deployer:
    network = paths.infra_env().get("INFRA_NETWORK", "infra")
    return Deployer(
        paths=paths,
        run=run,
        wait_healthy=lambda service: wait_healthy(service, timeout=timeout),
        network_name=network,
    )


def _print_record(record: history.DeployRecord) -> int:
    commit = (record.commit or "-")[:12]
    if record.succeeded:
        print(f"成功: {record.service} {record.action} {record.ref} ({commit})")
        return 0
    print(f"失敗: {record.service} {record.action} {record.ref} ({commit})", file=sys.stderr)
    print(f"  {record.message}", file=sys.stderr)
    return 1


def cmd_check(args: argparse.Namespace, paths: Paths) -> int:
    registry = _registry(paths)
    print(f"services.toml は正常です（{len(registry.services)} 件）")
    for service in sorted(registry.services, key=lambda s: s.port):
        kind = "基盤外" if service.external else "基盤"
        print(f"  {service.port:>5}  {service.name:<20} {kind}")
    return 0


def cmd_list(args: argparse.Namespace, paths: Paths) -> int:
    registry = _registry(paths)
    records = history.read_records(paths.history_file)
    for service in registry.services:
        current = history.last_success(records, service.name)
        version = f"{current.ref} ({(current.commit or '-')[:12]}) {current.time}" if current else "-"
        if service.external:
            version = "基盤外"
        print(f"{service.name:<20} :{service.port:<6} {version}")
    return 0


def cmd_deploy(args: argparse.Namespace, paths: Paths) -> int:
    service = _managed_service(_registry(paths), args.service)
    return _print_record(_deployer(paths, args.timeout).deploy(service, args.ref))


def cmd_rollback(args: argparse.Namespace, paths: Paths) -> int:
    service = _managed_service(_registry(paths), args.service)
    return _print_record(_deployer(paths, args.timeout).rollback(service))


def cmd_history(args: argparse.Namespace, paths: Paths) -> int:
    records = history.read_records(paths.history_file)
    if args.service:
        records = [r for r in records if r.service == args.service]
    for r in records[-args.n :]:
        mark = "OK " if r.succeeded else "NG "
        print(f"{mark} {r.time} {r.service:<16} {r.action:<8} {r.ref} ({(r.commit or '-')[:12]})")
        if r.message and not r.succeeded:
            print(f"     {r.message}")
    return 0


def cmd_core(args: argparse.Namespace, paths: Paths) -> int:
    import grp

    _registry(paths)  # 台帳が壊れたままポータルを起動しないように先に検査する
    env = os.environ.copy()
    env.update(paths.infra_env())
    env["DOCKER_GID"] = str(grp.getgrnam("docker").gr_gid)
    base = [
        "docker", "compose",
        "--project-name", CORE_PROJECT,
        "--file", str(paths.core_compose_file),
        "--env-file", str(paths.infra_env_file),
    ]  # fmt: skip
    actions = {
        "up": ["up", "--detach", "--build", "--remove-orphans"],
        "down": ["down"],
        "ps": ["ps"],
    }
    run([*base, *actions[args.action]], env=env)
    return 0


def cmd_db(args: argparse.Namespace, paths: Paths) -> int:
    service = _managed_service(_registry(paths), args.service)
    if not service.database:
        raise ValueError(f"{service.name} は services.toml で database = true になっていません")
    secret_file, created = database.create_database(paths, run, service.name)
    if created:
        print(f"DB {service.name} を作成し、接続先を {secret_file} に保存しました")
    else:
        print(f"{secret_file} が既にあるため、何もしませんでした")
    return 0


def cmd_backup(args: argparse.Namespace, paths: Paths) -> int:
    dest = database.backup(paths, run, datetime.now(UTC).astimezone(), keep=args.keep)
    print(f"バックアップしました: {dest}")
    return 0


def cmd_restore(args: argparse.Namespace, paths: Paths) -> int:
    if not args.yes:
        answer = input(f"DB {args.database} を {args.stamp} の内容で上書きします。よろしいですか？ [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("中止しました")
            return 1
    dump = database.restore(paths, run, args.stamp, args.database)
    print(f"{dump} から {args.database} を復元しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
