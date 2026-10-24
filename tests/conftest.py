import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrabox.settings import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        repo_root=tmp_path / "repo",
        etc_dir=tmp_path / "etc",
        state_dir=tmp_path / "state",
        backup_dir=tmp_path / "backup",
    )
