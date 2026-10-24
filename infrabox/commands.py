"""外部コマンド（git、docker）の実行。テストでは同じ形の関数に差し替える。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class CommandError(Exception):
    pass


class Runner(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        capture: bool = False,
    ) -> str: ...


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
    capture: bool = False,
) -> str:
    """コマンドを実行し、capture=True なら標準出力を返す。失敗したら CommandError を送出する。"""
    stdin = stdin_path.open("rb") if stdin_path else None
    stdout = stdout_path.open("wb") if stdout_path else (subprocess.PIPE if capture else None)
    try:
        completed = subprocess.run(
            args,
            env=env,
            input=input_text.encode() if input_text is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE if capture or stdout_path else None,
            check=False,
        )
    except FileNotFoundError:
        raise CommandError(f"コマンドが見つかりません: {args[0]}") from None
    finally:
        if stdin:
            stdin.close()
        if stdout_path and stdout:
            stdout.close()
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip() if completed.stderr else ""
        raise CommandError(f"{' '.join(args[:3])} … が終了コード {completed.returncode} で失敗しました。{detail}")
    if capture and completed.stdout is not None:
        return completed.stdout.decode(errors="replace")
    return ""
