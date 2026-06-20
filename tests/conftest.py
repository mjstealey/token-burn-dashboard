import json
from pathlib import Path

import pytest

from token_dashboard.db import Database
from token_dashboard.pricing import Pricing

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(str(REPO_ROOT / "pricing.yaml"))


@pytest.fixture
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
