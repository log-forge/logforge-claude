import sys
import pytest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk/python"))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def isolate_publisher_usage(tmp_path, monkeypatch):
    # Running packaged tests must never alter the user's real usage preferences or metrics.
    monkeypatch.setenv("LOGFORGE_USAGE_STATE", str(tmp_path / "test-usage.sqlite3"))
