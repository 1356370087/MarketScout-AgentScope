from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from open_deep_research.configuration import (
    RUN_CONFIG_FROZEN_FIELDS,
    RUN_CONFIG_FROZEN_FIELDS_V4,
    RUN_CONFIG_SCHEMA_VERSION,
    Configuration,
    freeze_run_config,
)
from open_deep_research.events.public import sanitize_public_payload
from open_deep_research.events.task_activity import sanitize_task_activity_payload
from open_deep_research.models.circuit import (
    CircuitFailureKind,
    CircuitOpenError,
    ModelCircuitBreaker,
    ModelCircuitPolicy,
    ModelCircuitState,
    _reset_model_circuit_registry,
    get_model_circuit_registry,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def policy(**overrides) -> ModelCircuitPolicy:
    values = {
        "failure_threshold": 2,
        "failure_window_seconds": 300.0,
        "open_cooldown_seconds": 60.0,
        "max_cooldown_seconds": 600.0,
        "slow_ratio_threshold": 0.5,
        "slow_min_samples": 4,
        "first_packet_probe": "shadow",
        "slow_first_packet_threshold_seconds": 8.0,
    }
    values.update(overrides)
    return ModelCircuitPolicy(**values)


@pytest.mark.asyncio
async def test_failure_threshold_opens_and_window_expiry_prunes() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker("openai:test", policy(), now=clock)

    first, _ = await breaker.before_call()
    assert await breaker.record_failure(
        first,
        failure_kind=CircuitFailureKind.RATE_LIMITED,
    ) is None
    assert (await breaker.snapshot()).state is ModelCircuitState.CLOSED

    clock.advance(301)
    second, _ = await breaker.before_call()
    assert await breaker.record_failure(
        second,
        failure_kind=CircuitFailureKind.TRANSIENT,
    ) is None
    snapshot = await breaker.snapshot()
    assert snapshot.state is ModelCircuitState.CLOSED
    assert snapshot.failure_count == 1

    third, _ = await breaker.before_call()
    transition = await breaker.record_failure(
        third,
        failure_kind=CircuitFailureKind.MODEL_UNAVAILABLE,
    )
    assert transition is not None
    assert transition.to_state is ModelCircuitState.OPEN
    assert transition.failure_count == 2


@pytest.mark.asyncio
async def test_open_rejects_then_allows_one_half_open_probe() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker(
        "openai:test",
        policy(failure_threshold=1),
        now=clock,
    )
    permit, _ = await breaker.before_call()
    await breaker.record_failure(
        permit,
        failure_kind=CircuitFailureKind.RATE_LIMITED,
    )

    with pytest.raises(CircuitOpenError) as rejected:
        await breaker.before_call()
    assert rejected.value.retry_after_seconds == 60.0

    clock.advance(60)

    async def attempt():
        try:
            return await breaker.before_call()
        except CircuitOpenError as exc:
            return exc

    results = await asyncio.gather(attempt(), attempt(), attempt())
    permits = [result for result in results if isinstance(result, tuple)]
    errors = [result for result in results if isinstance(result, CircuitOpenError)]
    assert len(permits) == 1
    assert permits[0][0].is_probe is True
    assert len(errors) == 2
    assert all(error.reason == "half_open_probe_in_flight" for error in errors)


@pytest.mark.asyncio
async def test_probe_success_closes_and_clears_windows() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker(
        "openai:test",
        policy(failure_threshold=1),
        now=clock,
    )
    permit, _ = await breaker.before_call()
    await breaker.record_failure(
        permit,
        failure_kind=CircuitFailureKind.TRANSIENT,
    )
    clock.advance(60)
    probe, transition = await breaker.before_call()
    assert transition is not None
    assert transition.to_state is ModelCircuitState.HALF_OPEN

    recovered = await breaker.record_success(probe, ttft_seconds=1.0)
    assert recovered is not None
    assert recovered.to_state is ModelCircuitState.CLOSED
    snapshot = await breaker.snapshot()
    assert snapshot.failure_count == 0
    assert snapshot.sample_count == 0
    assert snapshot.cooldown_seconds == 60.0


@pytest.mark.asyncio
async def test_probe_failures_back_off_to_cap() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker(
        "openai:test",
        policy(failure_threshold=1),
        now=clock,
    )
    permit, _ = await breaker.before_call()
    await breaker.record_failure(
        permit,
        failure_kind=CircuitFailureKind.TRANSIENT,
    )
    assert (await breaker.snapshot()).cooldown_seconds == 60.0

    expected = [120.0, 240.0, 480.0, 600.0, 600.0]
    for cooldown in expected:
        current = await breaker.snapshot()
        clock.advance(current.cooldown_seconds)
        probe, _ = await breaker.before_call()
        transition = await breaker.record_failure(
            probe,
            failure_kind=CircuitFailureKind.MODEL_UNAVAILABLE,
        )
        assert transition is not None
        assert transition.cooldown_seconds == cooldown


@pytest.mark.asyncio
async def test_shadow_slow_samples_do_not_open() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker("openai:test", policy(), now=clock)
    for _ in range(6):
        permit, _ = await breaker.before_call()
        assert await breaker.record_success(permit, ttft_seconds=9.0) is None
    snapshot = await breaker.snapshot()
    assert snapshot.state is ModelCircuitState.CLOSED
    assert snapshot.slow_count == 6


@pytest.mark.asyncio
async def test_enforced_slow_ratio_opens_at_minimum_samples() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker(
        "openai:test",
        policy(first_packet_probe="enforced"),
        now=clock,
    )
    ttfts = [9.0, 1.0, 9.0, 1.0]
    transition = None
    for ttft in ttfts:
        permit, _ = await breaker.before_call()
        transition = await breaker.record_success(permit, ttft_seconds=ttft)
    assert transition is not None
    assert transition.reason == "slow_first_packet_ratio"
    assert transition.to_state is ModelCircuitState.OPEN


@pytest.mark.asyncio
async def test_stale_generation_outcome_cannot_close_new_state() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker(
        "openai:test",
        policy(failure_threshold=1),
        now=clock,
    )
    first, _ = await breaker.before_call()
    stale, _ = await breaker.before_call()
    await breaker.record_failure(
        first,
        failure_kind=CircuitFailureKind.RATE_LIMITED,
    )
    assert await breaker.record_success(stale, ttft_seconds=1.0) is None
    assert (await breaker.snapshot()).state is ModelCircuitState.OPEN


@pytest.mark.asyncio
async def test_inconclusive_probe_releases_single_flight_without_backoff() -> None:
    clock = FakeClock()
    breaker = ModelCircuitBreaker(
        "openai:test",
        policy(failure_threshold=1),
        now=clock,
    )
    permit, _ = await breaker.before_call()
    await breaker.record_failure(
        permit,
        failure_kind=CircuitFailureKind.TRANSIENT,
    )
    clock.advance(60)
    probe, _ = await breaker.before_call()
    transition = await breaker.record_inconclusive(probe)
    assert transition is not None
    assert transition.reason == "probe_inconclusive"
    snapshot = await breaker.snapshot()
    assert snapshot.state is ModelCircuitState.OPEN
    assert snapshot.probe_in_flight is False
    assert snapshot.cooldown_seconds == 60.0


@pytest.mark.asyncio
async def test_registry_forces_oldest_open_candidate() -> None:
    _reset_model_circuit_registry()
    clock = FakeClock()
    registry = get_model_circuit_registry()
    circuit_policy = policy(failure_threshold=1)
    first = registry.get_or_create("openai:first", circuit_policy, now=clock)
    second = registry.get_or_create("openai:second", circuit_policy, now=clock)
    assert first is not None and second is not None

    permit, _ = await first.before_call()
    await first.record_failure(
        permit,
        failure_kind=CircuitFailureKind.TRANSIENT,
    )
    clock.advance(10)
    permit, _ = await second.before_call()
    await second.record_failure(
        permit,
        failure_kind=CircuitFailureKind.TRANSIENT,
    )

    index, transition = await registry.select_candidate_index(
        ["openai:first", "openai:second"],
        circuit_policy,
    )
    assert index == 0
    assert transition is not None
    assert transition.forced_probe is True
    assert (await first.snapshot()).state is ModelCircuitState.HALF_OPEN


def test_registry_policy_mismatch_fails_open_without_replacement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reset_model_circuit_registry()
    registry = get_model_circuit_registry()
    original = policy(failure_threshold=2)
    changed = policy(failure_threshold=3)
    first = registry.get_or_create("openai:test", original)

    assert first is not None
    with caplog.at_level("WARNING", logger="open_deep_research.models.circuit"):
        assert registry.get_or_create("openai:test", changed) is None
        assert registry.get_or_create("openai:test", changed) is None
    assert registry.get("openai:test") is first
    mismatch_warnings = [
        record
        for record in caplog.records
        if "Model circuit policy mismatch" in record.getMessage()
    ]
    assert len(mismatch_warnings) == 1


def test_registry_reset_removes_breakers() -> None:
    _reset_model_circuit_registry()
    registry = get_model_circuit_registry()
    assert registry.get_or_create("openai:test", policy()) is not None
    _reset_model_circuit_registry()
    assert get_model_circuit_registry().get("openai:test") is None


def test_circuit_configuration_defaults_remain_frozen_in_v7() -> None:
    configuration = Configuration()
    assert RUN_CONFIG_SCHEMA_VERSION == 14
    assert configuration.model_circuit_breaker_enabled is True
    assert configuration.model_first_packet_probe == "shadow"

    frozen = freeze_run_config({"configurable": {}, "metadata": {}})
    assert frozen["metadata"]["run_config_schema_version"] == 14
    for field_name in (
        "model_circuit_breaker_enabled",
        "model_circuit_failure_threshold",
        "model_circuit_failure_window_seconds",
        "model_circuit_open_cooldown_seconds",
        "model_circuit_max_cooldown_seconds",
        "model_circuit_slow_ratio_threshold",
        "model_circuit_slow_min_samples",
        "model_first_packet_probe",
        "model_first_packet_timeout_seconds",
        "model_slow_first_packet_threshold_seconds",
    ):
        assert field_name in RUN_CONFIG_FROZEN_FIELDS
        assert field_name not in RUN_CONFIG_FROZEN_FIELDS_V4
        assert field_name in frozen["configurable"]


@pytest.mark.parametrize(
    "values",
    [
        {
            "model_circuit_open_cooldown_seconds": 61,
            "model_circuit_max_cooldown_seconds": 60,
        },
        {
            "model_slow_first_packet_threshold_seconds": 15,
            "model_first_packet_timeout_seconds": 15,
        },
    ],
)
def test_circuit_configuration_rejects_invalid_threshold_relationships(
    values,
) -> None:
    with pytest.raises(ValidationError):
        Configuration(**values)






















































def test_circuit_public_payload_allowlists_preserve_only_safe_fields() -> None:
    payload = {
        "provider": "openai",
        "model": "gpt-test",
        "from_state": "closed",
        "to_state": "open",
        "reason": "failure_threshold:rate_limited",
        "failure_count": 5,
        "slow_count": 0,
        "sample_count": 0,
        "slow_ratio": 0.0,
        "cooldown_seconds": 60.0,
        "forced_probe": False,
        "authorization": "Bearer secret",
        "messages": ["private"],
    }
    public = sanitize_public_payload("model.circuit_state", payload)
    assert public["to_state"] == "open"
    assert public["failure_count"] == 5
    assert "authorization" not in public
    assert "messages" not in public

    activity = sanitize_task_activity_payload("model.circuit_open", payload)
    assert activity == {
        "provider": "openai",
        "model": "gpt-test",
        "reason": "failure_threshold:rate_limited",
        "failure_count": 5,
        "slow_count": 0,
        "sample_count": 0,
        "cooldown_seconds": 60.0,
    }
