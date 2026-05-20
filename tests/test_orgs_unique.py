"""Guard that MAJOR_HF_ORGS has no duplicates."""
import monitor


def test_major_hf_orgs_unique():
    orgs = monitor.MAJOR_HF_ORGS
    duplicates = [o for o in set(orgs) if orgs.count(o) > 1]
    assert not duplicates, f"Duplicate orgs in MAJOR_HF_ORGS: {duplicates}"
