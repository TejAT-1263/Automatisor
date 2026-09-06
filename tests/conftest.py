import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from db.build_db import build, DEFAULT_DB_PATH  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def ensure_database():
    """Every test in this suite reads real data, not mocks - so the
    actual SQLite file has to exist. Build it once per test session if
    it isn't already there; never fabricate data in a test itself."""
    if not DEFAULT_DB_PATH.exists():
        build(DEFAULT_DB_PATH)
    yield
