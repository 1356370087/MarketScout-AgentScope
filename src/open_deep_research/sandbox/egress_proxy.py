"""Token-authenticated HTTP/CONNECT proxy with DNS/IP pinning."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from contextlib import suppress
from urllib.parse import urlsplit

from open_deep_research.configuration import Configuration
from open_deep_research.sandbox.approvals import SecurityApproval
from open_deep_research.sandbox.crypto import decode_task_token, validate_timestamp
from open_deep_research.sandbox.gateway import GatewayRuntime
from open_deep_research.sandbox.internal_api import (
    ApprovalConsumeRequest,
    ApprovalCreateRequest,
    EgressTargetCheckRequest,
)
from open_deep_research.sandbox.schema import (
    resolve_profile,
)


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(ip.is_global)


class GatewayEgressProxy:
    """Enforce a task's frozen network Profile for arbitrary TCP clients."""

    def __init__(self, runtime: GatewayRuntime) -> None:
        """Bind proxy authorization to the Gateway run/token registry."""
        self.runtime = runtime

    @staticmethod
    def _normalize_host(host: str) -> str:
        """Canonicalize IP literals and DNS IDNs before every policy lookup."""
        candidate = host.strip().rstrip(".")
        if not candidate or any(ord(character) < 33 for character in candidate):
            raise ValueError("sandbox_proxy_invalid_host")
        if candidate.startswith("[") and candidate.endswith("]"):
            candidate = candidate[1:-1]
        try:
            return ipaddress.ip_address(candidate).compressed.lower()
        except ValueError:
            pass
        try:
            normalized = candidate.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("sandbox_proxy_invalid_host") from exc
        if len(normalized) > 253 or any(
            not label or len(label) > 63 for label in normalized.split(".")
        ):
            raise ValueError("sandbox_proxy_invalid_host")
        return normalized

    async def _resolve(self, host: str, port: int, allow_private: bool) -> str:
        infos = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        )
        addresses = sorted({str(item[4][0]) for item in infos})
        if not addresses or (not allow_private and any(not _public_address(ip) for ip in addresses)):
            raise ValueError("sandbox_private_destination_denied")
        return addresses[0]

    async def _authorize(
        self,
        *,
        task_token: str,
        timestamp: float,
        nonce: str,
        host: str,
        port: int,
        method: str,
        operation_id: str,
    ) -> tuple[bool, str | None, bool]:
        validate_timestamp(timestamp)
        claims = decode_task_token(task_token, self.runtime.keys.task_token)
        self.runtime.nonces.consume(claims.jti, nonce, expires_at=claims.expires_at)
        context = self.runtime.runs.get(claims.run_id)
        if context is None or context.fence_token != claims.fence_token:
            raise ValueError("stale_fence")
        _bundle, _profile_id, profile = resolve_profile(
            Configuration.from_runnable_config(context.config)
        )
        if method != "CONNECT" and method not in profile.network.allow_http_methods:
            return False, None, profile.network.allow_private_destinations
        async def check():
            return await self.runtime._egress_precheck(run_id=claims.run_id,
                task_id=claims.task_id, fence_token=claims.fence_token, stage="researching",
                host=host, port=port, tool_name="proxy", capability="proxy.connect",
                operation_id=operation_id, profile=profile)
        precheck = await check()
        private = profile.network.allow_private_destinations
        if precheck.decision != "ask":
            return precheck.decision == "allow", None, private
        if profile.approval_policy == "never":
            return False, None, private
        target = {"domain": host, "port": port}
        req = self.runtime.internal.signed(EgressTargetCheckRequest, run_id=claims.run_id,
            fence_token=claims.fence_token, capability="proxy.connect", target=target)
        state = await self.runtime.internal.post("/internal/sandbox/egress/target/check", req)
        if state.get("decision") == "block_run":
            return False, None, private
        candidates = [item for item in state.get("approvals", [])
            if item.get("task_id") == claims.task_id
            and item.get("status") in {"pending", "resolved"}
            and (item.get("status") == "pending" or item.get("decision") == "allow_once")
            and (not state.get("decision") or item.get("requested_at", 0) > state.get("updated_at", 0))]
        approval = SecurityApproval.model_validate(candidates[0]) if candidates else None
        if approval is None:
            create = self.runtime.internal.signed(ApprovalCreateRequest,
                run_id=claims.run_id, task_id=claims.task_id, fence_token=claims.fence_token,
                kind="network", capability="proxy.connect", target=target,
                operation_id=f"{operation_id}:{nonce}", stage="researching",
                expires_at=min(claims.expires_at, time.time() + profile.resources.approval_timeout_seconds))
            approval = SecurityApproval.model_validate(await self.runtime.internal.post(
                "/internal/sandbox/approvals/request", create))
        if approval.decision == "allow_once" and approval.status == "resolved":
            consume = self.runtime.internal.signed(ApprovalConsumeRequest, run_id=claims.run_id,
                fence_token=claims.fence_token, approval_id=approval.approval_id,
                operation_id=approval.operation_id)
            await self.runtime.internal.post("/internal/sandbox/approvals/consume", consume)
            fresh = await self.runtime.internal.post("/internal/sandbox/egress/target/check", req)
            latest = await check()
            allowed = latest.decision != "deny" and fresh.get("version", 0) == state.get("version", 0)
            return allowed, approval.approval_id, private
        return False, approval.approval_id, private

    async def _connection_still_allowed(self, claims, host: str, port: int,
                                        operation_id: str, approval_id: str | None) -> bool:
        """Recheck authority after DNS, without consuming a second connection grant."""
        context = self.runtime.runs.get(claims.run_id)
        if context is None or context.fence_token != claims.fence_token:
            return False
        _, _, profile = resolve_profile(Configuration.from_runnable_config(context.config))
        latest = await self.runtime._egress_precheck(run_id=claims.run_id, task_id=claims.task_id,
            fence_token=claims.fence_token, stage="researching", host=host, port=port,
            tool_name="proxy", capability="proxy.connect", operation_id=operation_id, profile=profile)
        if latest.decision != "ask":
            return latest.decision == "allow"
        if not approval_id:
            return False
        request = self.runtime.internal.signed(EgressTargetCheckRequest, run_id=claims.run_id,
            fence_token=claims.fence_token, capability="proxy.connect", target={"domain": host, "port": port})
        state = await self.runtime.internal.post("/internal/sandbox/egress/target/check", request)
        return state.get("decision") != "block_run" and any(
            item.get("approval_id") == approval_id and item.get("decision") == "allow_once"
            and item.get("status") == "consumed"
            and (state.get("decision") != "revoke" or item.get("requested_at", 0) > state.get("updated_at", 0))
            for item in state.get("approvals", []))

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
        *,
        approval_id: str | None = None,
    ) -> None:
        body = json.dumps(
            {"error": message, "approval_id": approval_id}, separators=(",", ":")
        ).encode()
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            407: "Proxy Authentication Required",
            500: "Internal Server Error",
        }.get(status, "Error")
        headers = [
            f"HTTP/1.1 {status} {reason}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if approval_id and all(
            character.isalnum() or character == "-" for character in approval_id
        ):
            headers.append(f"X-Sandbox-Approval-Id: {approval_id}")
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
        await writer.drain()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Process one bounded HTTP or CONNECT proxy connection."""
        upstream_writer = None
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(raw) > 65_536:
                raise ValueError("sandbox_proxy_headers_too_large")
            lines = raw.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
            authorization = headers.get("proxy-authorization", "")
            if not authorization.startswith("Bearer "):
                await self._reply(writer, 407, "Proxy Authentication Required")
                return
            timestamp = float(headers.get("x-sandbox-timestamp", "0"))
            nonce = headers.get("x-sandbox-nonce", "")
            if method.upper() == "CONNECT":
                parsed_connect = urlsplit(f"//{target}")
                if not parsed_connect.hostname or parsed_connect.port is None:
                    raise ValueError("sandbox_proxy_invalid_target")
                host = parsed_connect.hostname
                port = parsed_connect.port
                path = ""
            else:
                parsed = urlsplit(target)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise ValueError("sandbox_proxy_invalid_target")
                host = parsed.hostname
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
            host = self._normalize_host(host)
            unverified_claims = decode_task_token(
                authorization[7:], self.runtime.keys.task_token
            )
            operation_id = (
                f"proxy:{unverified_claims.task_id}:{host.lower()}:{port}"
            )
            allowed, approval_id, allow_private = await self._authorize(
                task_token=authorization[7:],
                timestamp=timestamp,
                nonce=nonce,
                host=host,
                port=port,
                method=method.upper(),
                operation_id=operation_id,
            )
            if not allowed:
                await self._reply(writer, 403, "sandbox_egress_denied", approval_id=approval_id)
                return
            address = await self._resolve(host, port, allow_private)
            if not await self._connection_still_allowed(unverified_claims, host, port, operation_id, approval_id):
                await self._reply(writer, 403, "sandbox_egress_changed", approval_id=approval_id)
                return
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), timeout=10
            )
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                filtered = [
                    f"{method} {path} {version}",
                    *[
                        line
                        for line in lines[1:]
                        if line
                        and not line.lower().startswith(
                            ("proxy-authorization:", "x-sandbox-timestamp:", "x-sandbox-nonce:")
                        )
                    ],
                    "",
                    "",
                ]
                upstream_writer.write("\r\n".join(filtered).encode("iso-8859-1"))
                await upstream_writer.drain()

            async def copy(source, destination):
                try:
                    while data := await source.read(64 * 1024):
                        destination.write(data)
                        await destination.drain()
                finally:
                    with suppress(Exception):
                        destination.close()

            await asyncio.gather(
                copy(reader, upstream_writer),
                copy(upstream_reader, writer),
            )
        except Exception as exc:  # noqa: BLE001 - proxy must return a bounded denial
            with suppress(Exception):
                await self._reply(writer, 403, str(exc)[:200])
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
