"""Process-local API creation and SSE admission limits."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from open_deep_research.api_governance import ConnectionLimiter, FixedWindowRateLimiter
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import get_prometheus_metrics
from security.rbac import Principal

logger = logging.getLogger(__name__)


class LimitedStreamingResponse(StreamingResponse):
    """Release admission even when disconnect wins before iteration starts."""

    def __init__(self, content, *, admission, release_token, **kwargs):
        super().__init__(content, **kwargs)
        self.admission, self.release_token = admission, release_token

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.admission.connection_limiter.release(self.release_token)


def _user_identity(user: Principal) -> str:
    """Return the normalized authenticated identity used by run ownership checks."""
    return user.user_id


def _principal_kind(user: Principal) -> str:
    """Return a bounded metric label for authenticated principal provenance."""
    return "development" if user.user_id == "local-dev-user" else "authenticated"


def _api_governance_metrics(configurable: Configuration) -> Any:
    return get_prometheus_metrics(configurable)


def _observe_rate_limited(
    configurable: Configuration,
    dimension: str,
    user: Principal,
) -> None:
    metrics = _api_governance_metrics(configurable)
    if metrics is not None:
        with contextlib.suppress(Exception):
            metrics.observe_api_rate_limited(dimension, _principal_kind(user))
    logger.info(
        "API request rate limited",
        extra={
            "actor": _user_identity(user),
            "action": "api.rate_limited",
            "dimension": dimension,
            "principal_kind": _principal_kind(user),
        },
    )


def _observe_limiter_error(
    configurable: Configuration,
    dimension: str,
    exc: BaseException,
) -> None:
    metrics = _api_governance_metrics(configurable)
    if metrics is not None:
        with contextlib.suppress(Exception):
            metrics.observe_rate_limiter_error(dimension)
    logger.warning(
        "API rate limiter failed open",
        extra={"action": "api.rate_limiter_error", "dimension": dimension},
        exc_info=exc,
    )


class ApiAdmission:
    """Share creation and SSE limits across native HTTP route families."""

    def __init__(self):
        self.rate_limiter = FixedWindowRateLimiter()
        self.connection_limiter = ConnectionLimiter()

    def enforce_creation(self, user: Principal, configurable: Configuration, active_runs: int) -> None:
        """Apply per-principal creation and active-run limits, failing open on bugs."""
        identity = _user_identity(user)
        try:
            allowed, retry_after = self.rate_limiter.allow(
                f"run-create:{identity}",
                configurable.api_run_create_per_minute,
            )
            if not allowed:
                _observe_rate_limited(configurable, "run_create_rate", user)
                raise HTTPException(
                    status_code=429,
                    detail="run_create_rate_limited",
                    headers={"Retry-After": str(retry_after)},
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - business limiter is fail-open
            _observe_limiter_error(configurable, "run_create_rate", exc)

        try:
            maximum = configurable.max_concurrent_runs_per_user
            if maximum > 0 and active_runs >= maximum:
                _observe_rate_limited(configurable, "concurrent_runs", user)
                raise HTTPException(
                    status_code=429,
                    detail="concurrent_run_limit_reached",
                    headers={"Retry-After": "5"},
                )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - business limiter is fail-open
            _observe_limiter_error(configurable, "concurrent_runs", exc)

    async def _reserve_sse_connection(
        self,
        user: Principal,
        configurable: Configuration,
    ) -> int:
        """Reserve one global SSE slot and return the release token (configured cap)."""
        limit = configurable.max_concurrent_sse_connections
        try:
            allowed = await self.connection_limiter.acquire(limit)
        except Exception as exc:  # noqa: BLE001 - business limiter is fail-open
            _observe_limiter_error(configurable, "sse_connections", exc)
            return 0
        if not allowed:
            _observe_rate_limited(configurable, "sse_connections", user)
            raise HTTPException(
                status_code=429,
                detail="sse_connection_limit_reached",
                headers={"Retry-After": "5"},
            )
        return limit

    async def _limited_sse(self, source: Any, release_token: int):
        """Release a global SSE slot whenever iteration ends or disconnects."""
        try:
            async for item in source:
                yield item
        finally:
            with contextlib.suppress(Exception):
                await self.connection_limiter.release(release_token)
