"""Hosted launches (fal / Artificial Analysis) keep reserved digest slots."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _hf(i):
    return monitor.ModelRelease(
        name=f"org/model-{i}", provider="Org", source="huggingface",
        url=f"https://example.com/hf/{i}", description="", downloads=100000 - i)


def _hosted(i, source="fal"):
    return monitor.ModelRelease(
        name=f"Hosted {i}", provider="Vendor", source=source,
        url=f"https://example.com/hosted/{i}", description="")


def _names(models):
    return [m.name for m in models]


def test_two_hosted_at_bottom_are_reserved():
    ranked = [_hf(i) for i in range(14)] + [_hosted(1), _hosted(2, "artificial-analysis")]
    picked, held = monitor._select_digest_candidates(ranked)
    assert len(picked) == 12
    assert "Hosted 1" in _names(picked) and "Hosted 2" in _names(picked)
    assert _names(held)[:2] == ["org/model-10", "org/model-11"]
    assert _names(picked)[:10] == _names(ranked)[:10]
    assert _names(picked)[-2:] == ["Hosted 1", "Hosted 2"]
    assert len(held) == 4


def test_only_one_hosted_available_reserves_one():
    ranked = [_hf(i) for i in range(14)] + [_hosted(1, "x-artificial-analysis")]
    picked, held = monitor._select_digest_candidates(ranked)
    assert len(picked) == 12
    assert sum(m.source != "huggingface" for m in picked) == 1
    assert "org/model-10" in _names(picked)
    assert "org/model-11" in _names(held)


def test_no_hosted_is_plain_slice():
    ranked = [_hf(i) for i in range(15)]
    picked, held = monitor._select_digest_candidates(ranked)
    assert picked == ranked[:12] and held == ranked[12:]


def test_hosted_already_in_top_is_unchanged():
    ranked = [_hosted(1), _hosted(2)] + [_hf(i) for i in range(12)]
    picked, held = monitor._select_digest_candidates(ranked)
    assert picked == ranked[:12] and held == ranked[12:]


def test_no_duplicates_and_within_limit():
    ranked = [_hf(i) for i in range(20)] + [_hosted(i) for i in range(3)]
    picked, held = monitor._select_digest_candidates(ranked)
    assert len(picked) <= 12
    assert len(set(_names(picked))) == len(picked)
    assert sorted(_names(picked + held)) == sorted(_names(ranked))


def test_more_than_reserve_hosted_beyond_cutoff_takes_best_two():
    ranked = [_hf(i) for i in range(14)] + [_hosted(i) for i in range(1, 5)]
    picked, held = monitor._select_digest_candidates(ranked)
    assert len(picked) == 12
    hosted_picked = [m.name for m in picked if m.source == "fal"]
    assert hosted_picked == ["Hosted 1", "Hosted 2"]
    assert "Hosted 3" in _names(held) and "Hosted 4" in _names(held)


def test_small_list_is_noop():
    ranked = [_hf(i) for i in range(5)] + [_hosted(1)]
    picked, held = monitor._select_digest_candidates(ranked)
    assert picked == ranked and held == []
