"""真实 TCP + 独立解释器退出；MockTransport 无法覆盖网络生成器关闭。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize(
    "checks", ["stream", "disconnect", "cancel", "stream,disconnect,cancel"]
)
def test_live_probe_stream_shutdown(tmp_path, checks):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            valid = self.headers.get("Authorization") == "Bearer fixture"
            body = json.dumps({"data": [{"id": "test"}]}).encode()
            self.send_response(200 if valid else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            parts = [
                {
                    "choices": [
                        {"index": 0, "delta": {"content": "OK"}, "finish_reason": None}
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            ]
            try:
                for part in parts:
                    data = {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "test",
                        **part,
                    }
                    chunk = ("data: " + json.dumps(data) + "\n\n").encode()
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                chunk = b"data: [DONE]\n\n"
                self.wfile.write(
                    f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n0\r\n\r\n"
                )
                self.wfile.flush()
            except BrokenPipeError, ConnectionResetError:
                # 消费者提前关闭是本测试的预期路径。
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[2]
    (tmp_path / ".env").write_text(
        f"LITELLM_BASE_URL=http://127.0.0.1:{server.server_port}/v1\n"
        "LITELLM_SERVICE_KEY=fixture\nLITELLM_SUMMARIZATION_MODEL=test\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "AS_LIVE_CHECKS": checks,
        "AS_LIVE_OUTPUT": str(tmp_path / "result.json"),
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(root / "tests/as_runtime/probe_litellm_live.py"),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "asynchronous generator" not in completed.stderr
    assert "RuntimeError" not in completed.stderr
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert not result.get("async_generator_errors")
    assert len(requests) == len(checks.split(","))
    assert all(check.get("passed", True) for check in result["checks"])
