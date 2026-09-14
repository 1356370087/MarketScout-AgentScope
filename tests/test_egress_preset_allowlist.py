"""Tests for the SANDBOX_EGRESS_ALLOW_DOMAINS preset allowlist."""

from pathlib import Path

from open_deep_research.sandbox.schema import (
    EGRESS_ALLOWLIST_ENV,
    load_policy_bundle,
    network_target_decision,
    policy_digest,
)

_REPO_POLICY = Path(__file__).resolve().parents[1] / "config" / "sandbox-policy.toml"


def test_preset_allowlist_merges_into_every_profile(monkeypatch) -> None:
    monkeypatch.setenv(
        EGRESS_ALLOWLIST_ENV,
        "www.gov.cn, *.epa.gov ,,Www.IEA.org",
    )
    bundle = load_policy_bundle(_REPO_POLICY)
    for profile in bundle.profiles.values():
        assert "www.gov.cn" in profile.network.allow_domains
        assert "*.epa.gov" in profile.network.allow_domains
        assert "www.iea.org" in profile.network.allow_domains

    decision = network_target_decision(
        bundle.profiles["research-gateway-only"].network,
        "www.gov.cn",
        443,
    )
    assert decision == "allow"


def test_preset_allowlist_wildcard_covers_subdomains(monkeypatch) -> None:
    monkeypatch.setenv(EGRESS_ALLOWLIST_ENV, "*.epa.gov")
    profile = load_policy_bundle(_REPO_POLICY).profiles["research-gateway-only"]
    assert network_target_decision(profile.network, "www.epa.gov", 443) == "allow"
    assert network_target_decision(profile.network, "ftp.epa.gov", 443) == "allow"
    # A sibling TLD must not leak through the pattern.
    assert network_target_decision(profile.network, "epa.gov.evil.com", 443) != "allow"


def test_preset_allowlist_participates_in_policy_digest(monkeypatch) -> None:
    without = policy_digest(load_policy_bundle(_REPO_POLICY))
    monkeypatch.setenv(EGRESS_ALLOWLIST_ENV, "www.gov.cn")
    with_preset = policy_digest(load_policy_bundle(_REPO_POLICY))
    assert without != with_preset


def test_unset_allowlist_leaves_policy_untouched(monkeypatch) -> None:
    monkeypatch.delenv(EGRESS_ALLOWLIST_ENV, raising=False)
    bundle = load_policy_bundle(_REPO_POLICY)
    assert "www.gov.cn" not in bundle.profiles[
        "research-gateway-only"
    ].network.allow_domains
