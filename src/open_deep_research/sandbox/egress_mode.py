"""Pure synthesis of the effective egress approval mode.

Three layers combine, from widest to narrowest authority:

1. Administrator TOML baseline (``NetworkPolicy.unknown_target``), frozen
   with the run; it caps every relaxation below it.
2. Run-level configuration (``sandbox_egress_approval_mode``), frozen with
   the run; ``"profile"`` means "no opinion, follow the baseline".
3. Runtime override (``manual``/``auto``/``open``), switchable mid-run via
   the run egress-mode API.

The effective mode is the narrowest of the expressed opinions. Widening a
request beyond the baseline is rejected fail-closed: the baseline wins and
the caller is expected to surface a warning. Narrowing is always allowed
and takes effect immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from open_deep_research.sandbox.schema import NetworkPolicy

# User-selectable modes (a run may switch between these at runtime).
EgressApprovalMode = Literal["manual", "auto", "open"]

# Modes including the deny baseline that only the administrator TOML can
# express; ``deny`` cannot be relaxed by any lower layer.
EgressBaselineMode = Literal["deny", "manual", "auto", "open"]

# Run-level frozen setting; ``profile`` delegates to the TOML baseline.
SandboxEgressModeSetting = Literal["profile", "manual", "auto", "open"]

# Narrowness ordering used to combine layers; wider modes have larger width.
_EGRESS_MODE_WIDTH: dict[str, int] = {
    "deny": 0,
    "manual": 1,
    "auto": 2,
    "open": 3,
}

_UNKNOWN_TARGET_TO_MODE: dict[str, str] = {
    "deny": "deny",
    "ask": "manual",
    "auto": "auto",
    "allow": "open",
}

RUN_EGRESS_MODE_VALUES: frozenset[str] = frozenset({"manual", "auto", "open"})


@dataclass(frozen=True)
class EffectiveEgressMode:
    """Resolved egress approval mode plus how it relates to the request."""

    mode: EgressBaselineMode
    # Runtime override as received (None when absent). When ``capped`` is
    # True this wider request was rejected by the TOML baseline.
    requested: str | None = None
    # True when a run/runtime request widened beyond the TOML baseline and
    # was narrowed back to it fail-closed.
    capped: bool = False

    @property
    def engages_classifier(self) -> bool:
        """Whether unknown targets should consult the egress classifier."""
        return self.mode == "auto"


def policy_baseline_mode(policy: NetworkPolicy) -> EgressBaselineMode:
    """Map the frozen TOML ``unknown_target`` onto the mode ordering."""
    mode = _UNKNOWN_TARGET_TO_MODE.get(policy.unknown_target)
    if mode is None:  # pragma: no cover - Literal makes this unreachable
        raise ValueError(f"sandbox_egress_mode_invalid_baseline:{policy.unknown_target}")
    return mode  # type: ignore[return-value]


def _checked_mode(value: str, *, label: str) -> str:
    if value not in _EGRESS_MODE_WIDTH:
        raise ValueError(f"sandbox_egress_mode_invalid_{label}:{value}")
    return value


def effective_egress_mode(
    baseline: EgressBaselineMode,
    run_setting: str = "profile",
    runtime_override: str | None = None,
) -> EffectiveEgressMode:
    """Combine the three mode layers into the narrowest effective mode.

    Args:
        baseline: Mode derived from the administrator TOML profile.
        run_setting: Frozen run-level setting; ``"profile"`` adds no opinion.
        runtime_override: Optional mid-run switch requested via the API.

    Returns:
        The effective mode plus capping diagnostics for warning surfaces.

    Raises:
        ValueError: If any layer carries an unknown mode value.
    """
    baseline_mode = _checked_mode(baseline, label="baseline")
    baseline_width = _EGRESS_MODE_WIDTH[baseline_mode]
    if run_setting != "profile":
        _checked_mode(run_setting, label="run_setting")
        if _EGRESS_MODE_WIDTH[run_setting] > baseline_width:
            run_setting = "profile"
    requested = None
    capped = False
    if runtime_override is not None:
        requested = _checked_mode(runtime_override, label="runtime_override")
        if _EGRESS_MODE_WIDTH[requested] > baseline_width:
            capped = True
            requested = None
    opinions = [baseline_width]
    if run_setting != "profile":
        opinions.append(_EGRESS_MODE_WIDTH[run_setting])
    # A None ``requested`` (capped) contributes nothing; the baseline stands.
    if requested is not None:
        opinions.append(_EGRESS_MODE_WIDTH[requested])
    width = min(opinions)
    mode = next(name for name, value in _EGRESS_MODE_WIDTH.items() if value == width)
    return EffectiveEgressMode(
        mode=mode,  # type: ignore[arg-type]
        requested=runtime_override,
        capped=capped,
    )
