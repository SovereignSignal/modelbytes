"""MAJOR_HF_ORGS casing and is_significant_release coverage.

HF's author= query is case-sensitive. Lowercase leftovers (inclusionai,
minimaxai, baai, bytedance-seed) return 0 models even though the org is
in KNOWN_ORGS (issues #24 / #28; live probe 2026-08-20).

is_significant_release kept a stale significant_orgs subset, so orgs added
since May only ranked via family tokens or ≥100k downloads (issue #15).
"""
import monitor


def test_major_hf_orgs_unique():
    orgs = monitor.MAJOR_HF_ORGS
    duplicates = [o for o in set(orgs) if orgs.count(o) > 1]
    assert not duplicates, f"Duplicate orgs in MAJOR_HF_ORGS: {duplicates}"


# HF-canonical mixed-case slugs. Lowercase copies of these return empty from
# the API and must not appear in MAJOR_HF_ORGS.
_HF_CANONICAL = {
    "inclusionAI": "inclusionai",
    "MiniMaxAI": "minimaxai",
    "BAAI": "baai",
    "ByteDance-Seed": "bytedance-seed",
}


def test_major_hf_orgs_uses_hf_canonical_casing():
    orgs = monitor.MAJOR_HF_ORGS
    for canonical, lowercase in _HF_CANONICAL.items():
        assert canonical in orgs, (
            f"{canonical!r} missing from MAJOR_HF_ORGS — HF author= is "
            f"case-sensitive and {lowercase!r} returns 0 models"
        )
        assert lowercase not in orgs, (
            f"{lowercase!r} is in MAJOR_HF_ORGS; the HF API returns empty "
            f"for that slug. Use {canonical!r}."
        )


def test_known_org_is_significant_without_downloads():
    # Issue #15: orgs in KNOWN_ORGS but not the stale significant_orgs list
    # must still rank as significant on day one (0 downloads, no family token).
    assert monitor.is_significant_release(
        "cohere/brand-new-flagship", "cohere", [], downloads=0) is True
    assert monitor.is_significant_release(
        "poolside/brand-new-flagship", "poolside", [], downloads=0) is True
    assert monitor.is_significant_release(
        "sapientinc/brand-new-flagship", "sapientinc", [], downloads=0) is True


def test_unknown_org_is_not_significant_without_signal():
    assert monitor.is_significant_release(
        "randomorg/mystery-9b", "randomorg", [], downloads=0) is False


def test_unknown_org_family_token_still_significant():
    assert monitor.is_significant_release(
        "randomorg/qwen3-8b-thing", "randomorg", [], downloads=0) is True


def test_unknown_org_high_downloads_still_significant():
    assert monitor.is_significant_release(
        "randomorg/mystery-9b", "randomorg", [], downloads=100_000) is True


def test_openrouter_tilde_slug_matches_known_org():
    # OpenRouter ids sometimes look like ~z-ai/glm-latest.
    assert monitor.is_significant_release(
        "~z-ai/glm-latest", "~z-ai", [], downloads=0) is True
