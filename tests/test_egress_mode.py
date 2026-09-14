"""Tests for the egress approval mode model and unknown-target expansion."""

from pathlib import Path

import pytest

from open_deep_research.sandbox.egress_mode import (
    EffectiveEgressMode,
    effective_egress_mode,
    policy_baseline_mode,
)
from open_deep_research.sandbox.schema import (
    NetworkPolicy,
    RuntimePolicy,
    SandboxProfile,
    load_policy_bundle,
    network_target_decision,
)


def _policy(unknown_target: str, **overrides) -> NetworkPolicy:
    return NetworkPolicy(unknown_target=unknown_target, **overrides)


class TestNetworkTargetDecisionExpansion:
    def test_auto_baseline_returns_ask_for_unmatched(self):
        assert network_target_decision(_policy("auto"), "example.com", 443) == "ask"

    def test_allow_baseline_admits_unmatched(self):
        assert network_target_decision(_policy("allow"), "example.com", 443) == "allow"

    def test_deny_baseline_rejects_unmatched(self):
        assert network_target_decision(_policy("deny"), "example.com", 443) == "deny"

    def test_allow_domains_still_allows_under_deny_baseline(self):
        policy = _policy("deny", allow_domains=["docs.example.org"])
        assert network_target_decision(policy, "docs.example.org", 443) == "allow"

    def test_deny_domains_beats_allow_baseline(self):
        policy = _policy("allow", deny_domains=["evil.example"])
        assert network_target_decision(policy, "evil.example", 443) == "deny"

    def test_port_violation_denies_even_under_allow_baseline(self):
        assert network_target_decision(_policy("allow"), "example.com", 8080) == "deny"

    def test_offline_mode_denies_even_under_allow_baseline(self):
        policy = _policy("allow", mode="offline")
        assert network_target_decision(policy, "example.com", 443) == "deny"

    def test_invalid_unknown_target_rejected(self):
        with pytest.raises(Exception):
            NetworkPolicy(unknown_target="sometimes")


class TestPolicyBaselineMode:
    def test_unknown_target_mapping(self):
        assert policy_baseline_mode(_policy("deny")) == "deny"
        assert policy_baseline_mode(_policy("ask")) == "manual"
        assert policy_baseline_mode(_policy("auto")) == "auto"
        assert policy_baseline_mode(_policy("allow")) == "open"


class TestEffectiveEgressMode:
    def test_profile_setting_follows_baseline(self):
        result = effective_egress_mode("manual", "profile", None)
        assert result == EffectiveEgressMode(mode="manual")

    def test_run_setting_can_narrow_baseline(self):
        result = effective_egress_mode("auto", "manual", None)
        assert result.mode == "manual"

    def test_run_setting_wider_than_baseline_is_ignored(self):
        result = effective_egress_mode("manual", "open", None)
        assert result.mode == "manual"
        assert not result.capped

    def test_runtime_override_narrows_immediately(self):
        result = effective_egress_mode("auto", "profile", "manual")
        assert result.mode == "manual"
        assert result.requested == "manual"
        assert not result.capped

    def test_runtime_override_wider_than_baseline_is_capped(self):
        result = effective_egress_mode("manual", "profile", "open")
        assert result.mode == "manual"
        assert result.requested == "open"
        assert result.capped

    def test_deny_baseline_blocks_every_relaxation(self):
        result = effective_egress_mode("deny", "profile", "open")
        assert result.mode == "deny"
        assert result.capped

    def test_narrowest_opinion_wins_across_layers(self):
        result = effective_egress_mode("open", "auto", "manual")
        assert result.mode == "manual"

    def test_engages_classifier_only_for_auto(self):
        assert effective_egress_mode("auto").engages_classifier
        assert not effective_egress_mode("manual").engages_classifier
        assert not effective_egress_mode("open").engages_classifier

    def test_unknown_values_raise(self):
        with pytest.raises(ValueError):
            effective_egress_mode("sometimes")
        with pytest.raises(ValueError):
            effective_egress_mode("manual", "yolo")
        with pytest.raises(ValueError):
            effective_egress_mode("manual", "profile", "yolo")


class TestPolicyBundleLoading:
    def test_default_bundle_loads_with_documented_baseline(self):
        bundle_path = Path(__file__).resolve().parents[1] / "config" / "sandbox-policy.toml"
        bundle = load_policy_bundle(bundle_path)
        profile = bundle.profiles[bundle.default_profile]
        assert profile.network.unknown_target == "auto"

    def test_profile_accepts_auto_and_allow_baselines(self):
        for value in ("auto", "allow"):
            policy = SandboxProfile(
                provider="docker",
                network=NetworkPolicy(unknown_target=value),
                runtime=RuntimePolicy(worker_image_digest="sha256:" + "0" * 64),
            )
            assert policy.network.unknown_target == value

    def test_inline_toml_with_auto_baseline(self, tmp_path: Path):
        document = (
            "version = 1\n"
            'deployment_id = "test-deploy"\n'
            'default_profile = "p"\n'
            '[profiles.p.network]\n'
            'unknown_target = "auto"\n'
            '[profiles.p.runtime]\n'
            'worker_image_digest = "sha256:' + "0" * 64 + '"\n'
        )
        path = tmp_path / "policy.toml"
        path.write_text(document, encoding="utf-8")
        bundle = load_policy_bundle(path)
        assert policy_baseline_mode(bundle.profiles["p"].network) == "auto"
