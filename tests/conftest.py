"""Every test run gets its own empty data directory and the built-in calendar."""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="sarvam-agent-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["GOOGLE_CALENDAR_TOKEN_PATH"] = os.path.join(_tmp, "no-token.json")

import pytest  # noqa: E402

from agent import db  # noqa: E402
from agent.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    settings.db_path.unlink(missing_ok=True)
    settings.runtime_path.unlink(missing_ok=True)
    db.init()
    yield
