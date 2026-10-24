"""デプロイ履歴（JSON Lines）の読み書き。

デプロイスクリプトが1行ずつ追記し、ポータルが読む。DBに置かないのは、
DBが止まったときにも履歴を見て原因を調べられるようにするため。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"


@dataclass(frozen=True)
class DeployRecord:
    time: str
    service: str
    action: str
    ref: str
    commit: str | None
    result: str
    rolled_back_to: str | None = None
    message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result == RESULT_SUCCESS


def append_record(path: Path, record: DeployRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(record), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_records(path: Path) -> list[DeployRecord]:
    """履歴を古い順に返す。壊れた行（書き込み途中の停電など）は読み飛ばす。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    fields = set(DeployRecord.__dataclass_fields__)
    records: list[DeployRecord] = []
    for line in text.splitlines():
        try:
            data = json.loads(line)
            records.append(DeployRecord(**{k: v for k, v in data.items() if k in fields}))
        except (json.JSONDecodeError, TypeError):
            continue
    return records


def last_success(records: list[DeployRecord], service: str) -> DeployRecord | None:
    """いま動いているはずの版（最後に成功したデプロイ）を返す。"""
    for record in reversed(records):
        if record.service == service and record.succeeded:
            return record
    return None


def previous_success(records: list[DeployRecord], service: str) -> DeployRecord | None:
    """いま動いている版の1つ前に成功した、commitの異なる版を返す。"""
    current = last_success(records, service)
    if current is None:
        return None
    for record in reversed(records):
        if record.service == service and record.succeeded and record.commit != current.commit:
            return record
    return None


def last_record(records: list[DeployRecord], service: str) -> DeployRecord | None:
    for record in reversed(records):
        if record.service == service:
            return record
    return None
