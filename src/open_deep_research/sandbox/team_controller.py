"""Trusted team containers use the existing authenticated Controller boundary."""

import asyncio
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from open_deep_research.sandbox.crypto import sign_payload


class TeamRequest(BaseModel):
    """Only task identity crosses the API boundary; deployment owns execution."""

    model_config = ConfigDict(extra="forbid")
    deployment_id: str
    lease: dict
    task_id: str
    stop: bool = False
    allow_start: bool = True
    service_timestamp: float
    service_nonce: str = Field(min_length=16, max_length=256)
    service_signature: str

    def signed_payload(self):
        return self.model_dump(mode="json", exclude={"service_signature"})


def install_team_routes(app, runtime):
    """Keep Docker access and administrator-selected credentials in Controller."""
    from docker.errors import NotFound

    lock = asyncio.Lock()

    def execute(request):
        runtime._authorize_service(request, operation="native-team")
        lease = request.lease
        name = (
            "insightforge-team-"
            + hashlib.sha256(
                f"{request.deployment_id}:{lease['run_id']}:{lease['fence']}:{request.task_id}".encode()
            ).hexdigest()[:24]
        )
        labels = {
            "insightforge.role": "native-team",
            "insightforge.deployment": request.deployment_id,
            "insightforge.run_id": lease["run_id"],
        }
        if os.environ.get("AS_TEAM_PROJECT"):
            labels["insightforge.project"] = os.environ["AS_TEAM_PROJECT"]
        try:
            container = runtime.client.containers.get(name)
        except NotFound:
            container = None
        if container is not None:
            if any(container.labels.get(k) != v for k, v in labels.items()):
                raise ValueError("team_container_ownership_mismatch")
            if request.stop:
                container.remove(force=True)
                return {"status": "stopped"}
            if container.status == "running":
                return {"status": "running"}
            if not request.allow_start:
                # Keep the exhausted attempt available for operational diagnosis.
                raise ValueError("team_container_restart_limit")
            container.remove()
        if request.stop:
            return {"status": "stopped"}
        if not request.allow_start:
            raise ValueError("team_container_restart_limit")
        from docker.types import Mount
        from dotenv import dotenv_values

        image = os.environ["AS_TEAM_WORKER_IMAGE"]
        env_file = Path(os.environ["AS_TEAM_WORKER_ENV_FILE"])
        if not env_file.is_file():
            raise ValueError("team_worker_environment_missing")
        environment = dict(dotenv_values(env_file))
        environment["AS_TEAM_EXECUTION"] = json.dumps(
            {"lease": lease, **({"member_id": request.task_id.removeprefix("member:")}
                if request.task_id.startswith("member:") else {"task_id": request.task_id})}
        )
        mounts = [
            Mount(**item)
            for item in json.loads(os.environ.get("AS_TEAM_CONTROLLER_MOUNTS", "[]"))
        ]
        runtime.client.containers.run(
            image,
            ["python", "-m", "open_deep_research.agentscope_runtime.team_executor"],
            name=name,
            detach=True,
            # 由本控制器在重启/关闭时删除，避免与 Docker 自动删除竞态；
            # 退出后的日志也得以保留到下一次健康检查。
            auto_remove=False,
            healthcheck={"test": ["NONE"]},
            labels=labels,
            environment=environment,
            network=os.environ.get("AS_TEAM_WORKER_NETWORK"),
            mounts=mounts,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
        )
        return {"status": "started"}

    @app.post("/v1/team/ensure")
    async def ensure_team(request: TeamRequest):
        try:
            async with lock:
                operation = asyncio.create_task(asyncio.to_thread(execute, request))
                try:
                    return await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # Docker's synchronous create keeps running after cancellation.
                    # Retain the lock until it ends so a subsequent stop can see it.
                    await asyncio.gather(operation, return_exceptions=True)
                    raise
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc


class ControllerTeamLauncher:
    """API-side launcher, with no Docker socket or executable dependency."""

    def __init__(self, client, *, max_restarts=3):
        self.client = client
        self.tasks = {}
        self.max_restarts = max_restarts

    async def _request(self, lease, task_id, *, stop=False, allow_start=True):
        request = TeamRequest(
            deployment_id=self.client.bundle.deployment_id,
            lease=asdict(lease),
            task_id=task_id,
            stop=stop,
            allow_start=allow_start,
            service_timestamp=time.time(),
            service_nonce=secrets.token_urlsafe(24),
            service_signature="pending",
        )
        request.service_signature = sign_payload(
            request.signed_payload(), self.client.keys.service_auth
        )
        return await self.client._post("/v1/team/ensure", request)

    async def ensure_started(self, lease, task_id, *, not_before=0):
        if time.time() < not_before:
            return
        key = (lease, task_id)
        attempts = self.tasks.setdefault(key, 0)
        result = await self._request(
            lease, task_id, allow_start=attempts < self.max_restarts + 1
        )
        if result["status"] == "started":
            self.tasks[key] += 1

    async def aclose(self):
        results = await asyncio.gather(
            *(
                self._request(lease, task_id, stop=True)
                for lease, task_id in self.tasks
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
