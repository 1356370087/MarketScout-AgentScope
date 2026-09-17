"""Launch trusted team executors in dedicated Docker containers.

The configured command must reconstruct TeamWorkers and call execute(task_id).
These control-plane containers are distinct from untrusted code sandboxes.
"""

import asyncio
import hashlib
import json
import time
from dataclasses import asdict


class DockerTeamLauncher:
    """Reconcile named containers; SQL task leases remain the commit authority."""

    def __init__(
        self, *, image, command, env_file, network=None, mounts=(), max_restarts=3
    ):
        self.image, self.command, self.env_file = image, tuple(command), str(env_file)
        self.network, self.mounts = network, tuple(mounts)
        self.max_restarts = max_restarts
        self.containers = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def docker(self, *args):
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _error = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        return process.returncode, output.decode().strip()

    async def ensure_started(self, lease, task_id, *, not_before=0):
        async with self.lock:
            if self.closed:
                raise RuntimeError("team launcher closed")
            name = (
                "insightforge-team-"
                + hashlib.sha256(
                    f"{lease.run_id}:{lease.fence}:{task_id}".encode()
                ).hexdigest()[:24]
            )
            attempts = self.containers.setdefault(name, 0)
            if time.time() < not_before:
                return
            code, status = await self.docker(
                "inspect", "--format", "{{.State.Running}}", name
            )
            if code == 0 and status == "true":
                return
            if attempts >= self.max_restarts + 1:
                raise RuntimeError("team_container_restart_limit")
            if code == 0:
                await self.docker("rm", name)
            request = json.dumps(
                {"lease": asdict(lease), "task_id": task_id}, separators=(",", ":")
            )
            args = [
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--label",
                "insightforge.role=native-team",
                "--env-file",
                self.env_file,
                "--env",
                "AS_TEAM_EXECUTION=" + request,
            ]
            if self.network:
                args.extend(["--network", self.network])
            for mount in self.mounts:
                args.extend(["--mount", mount])
            code, _ = await self.docker(*args, self.image, *self.command)
            if code:
                raise RuntimeError("team_container_start_failed")
            self.containers[name] = attempts + 1

    async def aclose(self):
        async with self.lock:
            self.closed = True
            for name in self.containers:
                await self.docker("rm", "--force", name)
            self.containers.clear()
