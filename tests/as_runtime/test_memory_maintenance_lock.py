"""Maintenance CLI releases its real file lock on both success and failure."""

from unittest.mock import Mock

import portalocker
import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.memory import maintenance


@pytest.mark.parametrize("fail", [False, True])
def test_maintenance_exit_releases_lock(monkeypatch, tmp_path, fail):
    config = Configuration(runs_dir=str(tmp_path))
    monkeypatch.setattr(maintenance, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        maintenance.Configuration, "from_runnable_config", Mock(return_value=config)
    )
    monkeypatch.setattr("sys.argv", ["maintenance", "daily"])
    lock_path = maintenance._command_lock_path(config, "daily")

    async def work(*args):
        with pytest.raises(portalocker.exceptions.LockException):
            with portalocker.Lock(str(lock_path), mode="a+b", timeout=0):
                pass
        if fail:
            raise RuntimeError("maintenance backend failed")
        return {"users": {}}

    monkeypatch.setattr(maintenance, "_run_daily", work)
    if fail:
        with pytest.raises(RuntimeError, match="backend failed"):
            maintenance.main()
    else:
        maintenance.main()
    with portalocker.Lock(str(lock_path), mode="a+b", timeout=0):
        pass
