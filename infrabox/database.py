"""共有PostgreSQLの操作（サービス用DBの作成、バックアップ、復元）。

psql などは `docker exec` で PostgreSQL コンテナの中で実行し、ホストにクライアントを入れない。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from .commands import Runner
from .registry import NAME_PATTERN
from .settings import Paths

POSTGRES_CONTAINER = "infrabox-postgres"
BACKUP_STAMP_PATTERN = re.compile(r"^\d{8}-\d{6}$")
GLOBALS_FILE = "globals.sql"

# 名前とパスワードは psql の変数で渡し、%I と %L でクォートしてから実行する。
# パスワードはコマンドライン引数に載せない（ps で見えるため）。
_CREATE_SQL = """\
\\set ON_ERROR_STOP on
\\set name '{name}'
\\set pw '{password}'
SELECT format('CREATE ROLE %I LOGIN', :'name')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'name') \\gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'name', :'pw') \\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'name', :'name')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'name') \\gexec
REVOKE ALL ON DATABASE :"name" FROM PUBLIC;
"""


def _psql(*args: str) -> list[str]:
    return ["docker", "exec", "-i", POSTGRES_CONTAINER, "psql", "-U", "postgres", "-d", "postgres", *args]


def create_database(paths: Paths, run: Runner, name: str) -> tuple[Path, bool]:
    """サービス用のDBとユーザーを作り、接続先を secrets に保存する。

    既に secrets がある場合は何もしない。戻り値は (secrets のパス, 新しく作ったか)。
    """
    if not NAME_PATTERN.match(name):
        raise ValueError(f"DB名に使えない名前です: {name}")
    secret_file = paths.secrets_dir / f"{name}.env"
    if secret_file.exists():
        return secret_file, False

    # token_urlsafe は英数字と - _ だけなので、psql の '...' の中にそのまま書ける
    password = secrets.token_urlsafe(32)
    run(_psql("-q", "-f", "-"), input_text=_CREATE_SQL.format(name=name, password=password))

    url = f"postgresql://{name}:{password}@{POSTGRES_CONTAINER}:5432/{name}"
    _write_private(secret_file, f"DATABASE_URL={url}\n")
    return secret_file, True


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def list_databases(run: Runner) -> list[str]:
    out = run(
        _psql(
            "-At", "-c", "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1"
        ),
        capture=True,
    )
    return [line for line in out.splitlines() if line]


def backup(paths: Paths, run: Runner, now: datetime, keep: int = 7) -> Path:
    """全DBを pg_dump し、新しい順に keep 世代だけ残す。"""
    root = paths.postgres_backup_dir
    root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    partial = root / f".{stamp}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(mode=0o750)

    run(
        ["docker", "exec", POSTGRES_CONTAINER, "pg_dumpall", "-U", "postgres", "--globals-only"],
        stdout_path=partial / GLOBALS_FILE,
    )
    for db in list_databases(run):
        run(
            ["docker", "exec", POSTGRES_CONTAINER, "pg_dump", "-U", "postgres", "--format=custom", db],
            stdout_path=partial / f"{db}.dump",
        )
    final = root / stamp
    partial.rename(final)
    prune_backups(root, keep)
    return final


def prune_backups(root: Path, keep: int) -> list[Path]:
    stamps = sorted(p for p in root.iterdir() if p.is_dir() and BACKUP_STAMP_PATTERN.match(p.name))
    removed = stamps[:-keep] if keep > 0 else stamps
    for path in removed:
        shutil.rmtree(path)
    # 途中で止まったバックアップの残骸も消す
    for path in root.glob(".*.partial"):
        shutil.rmtree(path)
    return removed


def restore(paths: Paths, run: Runner, stamp: str, database: str) -> Path:
    """バックアップから1つのDBを復元する。DBとユーザーは事前に存在している必要がある。"""
    if not BACKUP_STAMP_PATTERN.match(stamp):
        raise ValueError(f"バックアップ名は YYYYmmdd-HHMMSS の形式で指定してください: {stamp}")
    if not NAME_PATTERN.match(database):
        raise ValueError(f"DB名に使えない名前です: {database}")
    dump = paths.postgres_backup_dir / stamp / f"{database}.dump"
    if not dump.exists():
        raise FileNotFoundError(f"{dump} がありません")
    run(
        [
            "docker",
            "exec",
            "-i",
            POSTGRES_CONTAINER,
            "pg_restore",
            "-U",
            "postgres",
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "--dbname",
            database,
        ],
        stdin_path=dump,
    )
    return dump
