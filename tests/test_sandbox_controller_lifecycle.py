"""Controller lifecycle regressions at the Docker trust boundary."""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_deep_research.sandbox import controller_client
from open_deep_research.sandbox.controller import (
    DOCKER_ARCHIVE_FIXED_OVERHEAD_BYTES,
    DOCKER_ARCHIVE_OVERHEAD_PER_FILE_BYTES,
    DockerControllerRuntime,
)
from open_deep_research.sandbox.controller_client import SandboxControllerClient
from open_deep_research.sandbox.local_provider import BubblewrapSandboxProvider
from open_deep_research.sandbox.safe_io import UnsafeSandboxArchive


def _archive(name: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


def test_fifo_sentinel_read_has_a_hard_timeout() -> None:
    blocker = threading.Event()
    outcome: dict[str, BaseException] = {}

    class Container:
        @staticmethod
        def exec_run(*_args, **_kwargs):
            blocker.wait(2)
            return SimpleNamespace(exit_code=0, output=b"0")

    def invoke() -> None:
        try:
            DockerControllerRuntime._read_tmpfs_file(
                Container(),
                "/workspace/output/.worker-exit-code",
                max_bytes=16,
                timeout_seconds=0.05,
            )
        except BaseException as exc:  # noqa: BLE001 - assertion captures exact failure
            outcome["error"] = exc

    caller = threading.Thread(target=invoke, daemon=True)
    caller.start()
    caller.join(0.3)
    completed_before_release = not caller.is_alive()
    blocker.set()
    caller.join(1)

    assert completed_before_release is True
    assert isinstance(outcome.get("error"), TimeoutError)


def test_watchdog_checks_other_containers_when_one_status_probe_stalls() -> None:
    slow_release = threading.Event()
    fast_checked = threading.Event()
    slow = SimpleNamespace(id="slow")
    fast = SimpleNamespace(id="fast")
    runtime = object.__new__(DockerControllerRuntime)
    runtime.bundle = SimpleNamespace(deployment_id="deployment")
    runtime.client = SimpleNamespace(
        containers=SimpleNamespace(
            list=lambda **_kwargs: [slow, fast],
        )
    )

    def status(container):
        if container is slow:
            slow_release.wait(2)
        else:
            fast_checked.set()
        return SimpleNamespace(timed_out=False)

    runtime._status = status
    sweep = threading.Thread(target=runtime.sweep_expired, daemon=True)
    sweep.start()
    try:
        assert fast_checked.wait(0.3) is True
    finally:
        slow_release.set()
        sweep.join(1)


@pytest.mark.asyncio
async def test_collect_archive_client_has_a_finite_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        content = b"archive"

        @staticmethod
        def raise_for_status() -> None:
            return None

    class AsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(controller_client.httpx, "AsyncClient", AsyncClient)
    client = object.__new__(SandboxControllerClient)
    client.configurable = SimpleNamespace(sandbox_controller_socket="/tmp/controller.sock")
    client._task_request = lambda _container_id: SimpleNamespace(
        model_dump_json=lambda: "{}"
    )

    assert await client.collect_archive("container") == b"archive"
    assert captured["timeout"] is not None


@pytest.mark.asyncio
async def test_concurrent_controller_calls_do_not_close_each_others_transport(monkeypatch):
    import asyncio
    import httpx

    from open_deep_research.sandbox import controller_client

    transports = []
    slow_started, fast_closed = asyncio.Event(), asyncio.Event()

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.closed = False
            self.slow = False
            transports.append(self)

        async def handle_async_request(self, request):
            self.slow = request.url.path == "/slow"
            if self.slow:
                slow_started.set()
                await fast_closed.wait()
                assert not self.closed
            else:
                await slow_started.wait()
            return httpx.Response(200, json={"status": "ok"})

        async def aclose(self):
            self.closed = True
            if not self.slow:
                fast_closed.set()

    monkeypatch.setattr(controller_client.httpx, "AsyncHTTPTransport", Transport)
    client = object.__new__(SandboxControllerClient)
    client.configurable = SimpleNamespace(sandbox_controller_socket="/tmp/controller.sock")
    request = SimpleNamespace(model_dump_json=lambda: "{}")
    assert await asyncio.gather(client._post("/slow", request), client._post("/fast", request)) == [
        {"status": "ok"}, {"status": "ok"},
    ]
    assert len(transports) == 2 and all(item.closed for item in transports)


def test_collect_archive_works_after_worker_stops() -> None:
    class Container:
        id = "worker-stopped"

        @staticmethod
        def exec_run(*_args, **_kwargs):
            raise AssertionError("stopped containers cannot execute commands")

        @staticmethod
        def get_archive(path):
            prefix = path.rsplit("/", 1)[-1]
            return iter([_archive(f"{prefix}/result.txt", prefix.encode())]), {}

    runtime = object.__new__(DockerControllerRuntime)
    runtime._authorize_service = lambda *_args, **_kwargs: None
    runtime._owned_worker = lambda _container_id: Container()
    runtime._profile_for_container = lambda _container: SimpleNamespace(
        resources=SimpleNamespace(
            output_bytes=1024,
            log_bytes=1024,
            artifact_bytes=1024,
            max_files=10,
        )
    )
    runtime._status = lambda _container: SimpleNamespace(
        status="exited", exit_code=0
    )

    archive = runtime.collect_archive(SimpleNamespace(container_id="worker-stopped"))

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        assert bundle.getnames() == [
            "output/result.txt",
            "logs/result.txt",
            "artifacts/result.txt",
        ]


@pytest.mark.parametrize("status", ["exited", "result_ready"])
def test_collect_archive_rejects_oversized_raw_stream_and_releases_lease(status) -> None:
    content_limit = 3 * 1024
    raw_limit = (
        content_limit
        + DOCKER_ARCHIVE_FIXED_OVERHEAD_BYTES
        + DOCKER_ARCHIVE_OVERHEAD_PER_FILE_BYTES
    )

    class Container:
        id = "worker-oversized-archive"

        @staticmethod
        def exec_run(*_args, **_kwargs):
            return SimpleNamespace(exit_code=1, output=b"x" * (raw_limit + 1))

        @staticmethod
        def get_archive(_path):
            return iter([b"x" * (raw_limit + 1)]), {}

    runtime = object.__new__(DockerControllerRuntime)
    runtime._authorize_service = lambda *_args, **_kwargs: None
    runtime._owned_worker = lambda _container_id: Container()
    runtime._profile_for_container = lambda _container: SimpleNamespace(
        resources=SimpleNamespace(
            output_bytes=1024,
            log_bytes=1024,
            artifact_bytes=1024,
            max_files=1,
        )
    )
    runtime._status = lambda _container: SimpleNamespace(
        status=status, exit_code=0
    )
    runtime._begin_collection(Container.id)

    with pytest.raises(UnsafeSandboxArchive, match="archive bytes exceed limit"):
        runtime.collect_archive(SimpleNamespace(container_id=Container.id))

    assert runtime._collection_in_progress(Container.id) is False


def test_collection_lease_blocks_stop_until_archive_finishes() -> None:
    runtime = object.__new__(DockerControllerRuntime)
    runtime._collection_lock = threading.Lock()
    runtime._collection_leases = {}
    runtime._authorize_service = lambda *_args, **_kwargs: None
    container = SimpleNamespace(id="worker-collecting")
    runtime._owned_worker = lambda _container_id: container
    stopped: list[str] = []
    runtime._terminate = lambda item, **_kwargs: stopped.append(item.id)
    request = SimpleNamespace(container_id=container.id, timeout_seconds=1)

    runtime._begin_collection(container.id)
    runtime.stop(request)
    assert stopped == []

    runtime._finish_collection(container.id)
    runtime.stop(request)
    assert stopped == [container.id]


def test_collect_releases_collection_lease_when_collection_fails() -> None:
    class Container:
        id = "worker-collect-failed"

        @staticmethod
        def logs(**_kwargs):
            raise RuntimeError("docker log stream failed")

    runtime = object.__new__(DockerControllerRuntime)
    runtime._authorize_service = lambda *_args, **_kwargs: None
    runtime._owned_worker = lambda _container_id: Container()
    runtime._profile_for_container = lambda _container: SimpleNamespace(
        resources=SimpleNamespace(log_bytes=1024)
    )
    runtime._status = lambda _container: SimpleNamespace(
        status="exited", exit_code=0
    )

    with pytest.raises(RuntimeError, match="docker log stream failed"):
        runtime.collect(SimpleNamespace(container_id=Container.id))

    assert runtime._collection_in_progress(Container.id) is False


def test_result_ready_wins_at_exact_deadline_boundary() -> None:
    class Container:
        id = "ready"
        status = "running"
        attrs = {
            "Config": {
                "Labels": {
                    "com.insightforge.sandbox.deadline_at": str(time.time() - 1),
                }
            },
            "State": {},
        }

        @staticmethod
        def reload() -> None:
            return None

        @staticmethod
        def exec_run(command, **_kwargs):
            if command[0] == "/usr/bin/stat":
                return SimpleNamespace(exit_code=0, output=b"81a4:1")
            return SimpleNamespace(exit_code=0, output=b"0")

    runtime = object.__new__(DockerControllerRuntime)
    status = runtime._status(Container())

    assert status.status == "result_ready"
    assert status.timed_out is False


def test_deadline_is_measured_from_container_start_not_create() -> None:
    terminated: list[str] = []

    class Container:
        id = "started-recently"
        status = "running"
        attrs = {
            "Config": {
                "Labels": {
                    "com.insightforge.sandbox.deadline_at": str(time.time() - 1),
                }
            },
            "State": {
                "StartedAt": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
            },
        }

        @staticmethod
        def reload() -> None:
            return None

        @staticmethod
        def exec_run(_command, **_kwargs):
            return SimpleNamespace(exit_code=1, output=b"")

    runtime = object.__new__(DockerControllerRuntime)
    runtime._profile_for_container = lambda _container: SimpleNamespace(
        resources=SimpleNamespace(timeout_seconds=60, stop_grace_seconds=5)
    )
    runtime._terminate = lambda container, **_kwargs: terminated.append(container.id)

    status = runtime._status(Container())

    assert status.timed_out is False
    assert terminated == []


def test_existing_container_disposition_covers_all_fence_branches() -> None:
    disposition = DockerControllerRuntime._existing_container_disposition
    assert disposition("running", existing_fence=3, requested_fence=2) == "stale"
    assert disposition("running", existing_fence=2, requested_fence=2) == "reuse"
    assert disposition("exited", existing_fence=2, requested_fence=2) == "replace"
    assert disposition("running", existing_fence=1, requested_fence=2) == "replace"


def test_reconcile_rechecks_deployment_labels_before_mutation() -> None:
    actions: list[str] = []

    class Resource:
        def __init__(self, resource_id: str, deployment_id: str, kind: str) -> None:
            self.id = resource_id
            self.status = "running"
            labels = {
                "com.insightforge.sandbox.deployment_id": deployment_id,
                "com.insightforge.sandbox.resource_kind": kind,
                "com.insightforge.sandbox.run_id": f"run-{resource_id}",
                "com.insightforge.sandbox.task_id": f"task-{resource_id}",
                "com.insightforge.sandbox.fence_token": "1",
                "com.insightforge.sandbox.profile_id": "research-gateway-only",
                "com.insightforge.sandbox.deadline_at": "0",
            }
            self.attrs = (
                {"Config": {"Labels": labels}, "State": {}}
                if kind in {"worker", "seed"}
                else {"Labels": labels, "Containers": {}}
            )

        def reload(self) -> None:
            return None

        def remove(self, *, force: bool = False) -> None:
            actions.append(f"remove:{self.id}:{force}")

    owned_worker = Resource("owned-worker", "deployment-a", "worker")
    foreign_worker = Resource("foreign-worker", "deployment-b", "worker")
    owned_seed = Resource("owned-seed", "deployment-a", "seed")
    foreign_seed = Resource("foreign-seed", "deployment-b", "seed")
    owned_network = Resource("owned-network", "deployment-a", "network")
    foreign_network = Resource("foreign-network", "deployment-b", "network")
    owned_volume = Resource("owned-volume", "deployment-a", "input")
    foreign_volume = Resource("foreign-volume", "deployment-b", "input")
    observed_filters: list[dict] = []

    def list_containers(*, all: bool, filters: dict):
        del all
        observed_filters.append(filters)
        labels = filters["label"]
        if "com.insightforge.sandbox.resource_kind=worker" in labels:
            return [owned_worker, foreign_worker]
        return [owned_seed, foreign_seed]

    runtime = object.__new__(DockerControllerRuntime)
    runtime.bundle = SimpleNamespace(
        deployment_id="deployment-a",
        profiles={
            "research-gateway-only": SimpleNamespace(
                resources=SimpleNamespace(stop_grace_seconds=5)
            )
        },
    )
    runtime.client = SimpleNamespace(
        containers=SimpleNamespace(list=list_containers),
        networks=SimpleNamespace(
            list=lambda **_kwargs: [owned_network, foreign_network]
        ),
        volumes=SimpleNamespace(
            list=lambda **_kwargs: [owned_volume, foreign_volume]
        ),
    )
    runtime._authorize_service = lambda *_args, **_kwargs: None
    runtime._terminate = lambda container, **_kwargs: actions.append(
        f"terminate:{container.id}"
    )

    stopped = runtime.reconcile(SimpleNamespace(active_tasks=[]))

    assert stopped == ["owned-worker"]
    assert actions == [
        "terminate:owned-worker",
        "remove:owned-seed:True",
        "remove:owned-network:False",
        "remove:owned-volume:True",
    ]
    assert all(
        "com.insightforge.sandbox.deployment_id=deployment-a" in item["label"]
        for item in observed_filters
    )


def test_terminate_escalates_from_term_to_kill() -> None:
    calls: list[str] = []

    class Container:
        status = "running"

        @staticmethod
        def reload() -> None:
            return None

        @staticmethod
        def stop(*, timeout: int) -> None:
            calls.append(f"term:{timeout}")

        def kill(self, *, signal: str) -> None:
            calls.append(signal)
            self.status = "exited"

    runtime = object.__new__(DockerControllerRuntime)
    runtime._profile_for_container = lambda _container: SimpleNamespace(
        runtime=SimpleNamespace(retention="retain_stopped")
    )

    runtime._terminate(
        Container(),
        timeout_seconds=5,
        apply_retention=False,
    )

    assert calls == ["term:5", "SIGKILL"]


@pytest.mark.asyncio
async def test_cancel_during_native_team_create_serializes_stop(tmp_path, monkeypatch):
    from docker.errors import NotFound
    from fastapi import FastAPI
    from open_deep_research.sandbox.team_controller import TeamRequest, install_team_routes

    started, finish = threading.Event(), threading.Event()
    events = []
    container = None

    class Containers:
        def get(self, name):
            if container is None:
                raise NotFound("not created yet")
            return container

        def run(self, *_args, **kwargs):
            nonlocal container
            started.set()
            assert finish.wait(2), "test did not release Docker create"
            container = SimpleNamespace(labels=kwargs["labels"], status="running",
                                        remove=lambda **_: events.append("stopped"))
            events.append("created")
            return container

    environment = tmp_path / "worker.env"
    environment.write_text("APP_ENV=development\n", encoding="utf-8")
    monkeypatch.setenv("AS_TEAM_WORKER_ENV_FILE", str(environment))
    monkeypatch.setenv("AS_TEAM_WORKER_IMAGE", "fixture:local")
    app = FastAPI()
    install_team_routes(app, SimpleNamespace(
        _authorize_service=lambda *_a, **_kw: None,
        client=SimpleNamespace(containers=Containers()),
    ))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/team/ensure")
    request = TeamRequest(deployment_id="fixture", lease={"run_id": "run", "fence": 1},
                          task_id="task", service_timestamp=time.time(),
                          service_nonce="fixture-nonce-123456", service_signature="fixture")
    create = asyncio.create_task(endpoint(request))
    stop = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        create.cancel()
        stop = asyncio.create_task(endpoint(request.model_copy(update={"stop": True})))
        await asyncio.sleep(0.03)
        assert not stop.done(), "stop overtook an unfinished Docker create"
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await create
        assert await stop == {"status": "stopped"}
        assert events == ["created", "stopped"]
    finally:
        finish.set()
        await asyncio.gather(create, *([stop] if stop else []), return_exceptions=True)


def test_bwrap_does_not_bind_the_entire_host_root() -> None:
    argv = BubblewrapSandboxProvider._build_bubblewrap_argv(
        bwrap="/usr/bin/bwrap",
        workspace=Path("/safe/workspace"),
        working_dir=Path("/safe/workspace/subdir"),
        command="pwd",
    )

    triples = list(zip(argv, argv[1:], argv[2:]))
    assert ("--ro-bind", "/", "/") not in triples
    assert ("--bind", "/safe/workspace", "/workspace") in triples
