"""Real HTTP/SSE sockets and process restart over native durable run state."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.mark.parametrize("entrypoint", ["isolated", "application"])
def test_http_process_admission_disconnect_and_startup_resume(tmp_path, entrypoint):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)]),
           "MAX_CONCURRENT_RUNS_PER_USER": "1", "MAX_CONCURRENT_SSE_CONNECTIONS": "1",
           "API_RUN_CREATE_PER_MINUTE": "10", "TEST_APPLICATION_ENTRYPOINT": str(entrypoint == "application").lower(),
           "PYTHON_DOTENV_DISABLED": "1", "APP_ENV": "development", "LOCAL_DEV_AUTH_BYPASS": "true",
           "IAM_DATABASE_URL": "", "DOCUMENT_RESEARCH_ENABLED": "false", "MODEL_BACKEND": "legacy",
           "SANDBOX_ENABLED": "false", "RETENTION_SWEEP_INTERVAL_SECONDS": "0",
           "RUN_RECOVERY_SWEEP_ON_STARTUP": "true", "RESEARCH_ENGINE": "native", "RUNS_DIR": str(tmp_path),
           "TRACE_STORE_PATH": str(tmp_path / "traces.db")}
    processes = []

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise AssertionError("native process did not reach expected state")

    with (tmp_path / "host.log").open("wb") as log, httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
        def start():
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.as_runtime.native_lifecycle_host", str(tmp_path), str(port)],
                cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            processes.append(process)
            wait_for(lambda: client.get("/testing/health").status_code == 200)
            return process

        try:
            first_process = start()
            question = {"messages": [{"role": "user", "content": "Research fixture"}]}
            response = client.post("/runs", json=question)
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            wait_for(lambda: client.get(f"/runs/{run_id}").json()["status"] == "awaiting_plan_approval")
            assert client.post("/runs", json=question).status_code == 429
            with client.stream("GET", f"/runs/{run_id}/events") as events:
                assert events.status_code == 200
                lines = events.iter_lines()
                next(lines)
                with client.stream("GET", f"/runs/{run_id}/events") as blocked:
                    assert blocked.status_code == 429
                assert client.post("/runs/stream", json=question).status_code == 429
            wait_for(lambda: client.get("/testing/health").json()["connections"] == 0)
            snapshot = client.get(f"/runs/{run_id}").json()
            action = snapshot["pending_human_action"]["action_id"]
            assert client.post(f"/runs/{run_id}/human-actions/{action}", json={"action": "approve"}).status_code == 200
            wait_for(lambda: client.get(f"/runs/{run_id}").json()["status"] == "running")
            assert client.post("/testing/shutdown").status_code == 200
            assert first_process.wait(timeout=15) == 0
            (tmp_path / "continue").write_text("continue", encoding="utf-8")
            second_process = start()
            wait_for(lambda: client.get(f"/runs/{run_id}").json()["status"] == "completed")
            snapshot = client.get(f"/runs/{run_id}").json()
            assert snapshot["output"]["markdown"] == "Native process recovered successfully."
            assert client.get(f"/runs/{run_id}/events").status_code == 200
            assert client.get("/testing/health").json()["connections"] == 0
            assert client.delete(f"/runs/{run_id}?dry_run=true").json()["status"] == "would_delete"
            assert client.delete(f"/runs/{run_id}").status_code == 200
            assert client.get(f"/runs/{run_id}").status_code == 404
            assert client.post("/testing/shutdown").status_code == 200
            assert second_process.wait(timeout=15) == 0
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
    assert all(process.poll() is not None for process in processes)
