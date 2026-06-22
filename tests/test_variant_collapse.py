"""Variant-collapse rule for the inline digest path (2026-06-22 spec).

When an org drops a family of variants at the same base size (SFT variants,
dataset-named fine-tunes, training checkpoints), collapse N≥3 of them into ONE
entry so the daily cap isn't burned by one org's batch and the channel doesn't
read as spam. Decisions (Sov, 2026-06-22 Notion spec): threshold ≥3; collapsed
entry names the family + count + the variant suffixes; a significant variant
escapes the collapse; instruct/base pairs stay separate; runs only on the
inline path, not the curator.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor


def _m(name, **kw):
    fields = dict(
        provider=name.split("/")[0].title(),
        source="huggingface",
        url="https://huggingface.co/" + name,
        description="x",
    )
    fields.update(kw)
    return monitor.ModelRelease(name=name, **fields)


# ── the 2026-06-22 incident, precisely ──────────────────────────────────────

def test_six_same_size_variants_collapse_to_one():
    six = [_m(f"allenai/qwen35-9b-{v}") for v in
           ["termigen", "cli-gym", "swesmith", "terminaltraj", "endless", "openthoughts"]]
    out = monitor.collapse_variants(six)
    assert len(out) == 1
    entry = out[0]
    # names the family + the count
    assert "6" in (entry.description + entry.name)
    # names the variant suffixes
    assert "termigen" in entry.description and "openthoughts" in entry.description


def test_three_is_the_threshold():
    three = [_m(f"allenai/qwen35-9b-{v}") for v in ["a", "b", "c"]]
    assert len(monitor.collapse_variants(three)) == 1


def test_two_variants_stay_separate():
    # Decision 1: ≥3 to collapse. Two might be a real instruct/base pair.
    two = [_m("x/Foo-8b"), _m("x/Foo-8b-math")]
    out = monitor.collapse_variants(two)
    assert len(out) == 2


def test_different_sizes_stay_separate():
    # Decision / spec core: size tiers are genuinely different models.
    sizes = [_m("allenai/tmax-27b"), _m("allenai/tmax-8b"), _m("allenai/tmax-2b")]
    out = monitor.collapse_variants(sizes)
    assert len(out) == 3


def test_instruct_base_pair_stays_separate():
    # Decision 5: instruct vs base at the same size is a real capability split.
    pair = [_m("meta-llama/Llama-4-70B"), _m("meta-llama/Llama-4-70B-Instruct"),
            _m("meta-llama/Llama-4-70B-Base")]
    out = monitor.collapse_variants(pair)
    # None of these have a variant-suffix pattern that collapses; they must
    # all remain separate (instruct/base/it/chat are protected tiers).
    assert len(out) == 3


def test_significant_variant_escapes_the_collapse():
    # Decision 3: a variant that is_significant_release surfaces on its own,
    # and the rest still collapse.
    fam = [_m(f"allenai/qwen35-9b-{v}") for v in ["a", "b", "c", "d"]]
    # mark one as significant via a known family + engagement
    fam[0] = _m("allenai/qwen35-9b-a", downloads=50000, likes=2000)
    out = monitor.collapse_variants(fam)
    # one significant singleton + one collapsed-family entry (the other 3)
    assert len(out) == 2
    assert any(o.name == "allenai/qwen35-9b-a" for o in out)


def test_finetune_family_collapses():
    # Decision 6: Math/Code/domain fine-tunes of the same base collapse at ≥3.
    fam = [_m("x/Llama-4-8B"), _m("x/Llama-4-8B-Math"),
           _m("x/Llama-4-8B-Code"), _m("x/Llama-4-8B-Vision")]
    out = monitor.collapse_variants(fam)
    assert len(out) == 1


def test_mixed_groups_collapse_independently():
    # Two distinct families + a singleton all in one batch.
    batch = (
        [_m(f"allenai/qwen35-9b-{v}") for v in ["a", "b", "c"]] +
        [_m(f"otherorg/foomod-7b-{v}") for v in ["x", "y", "z"]] +
        [_m("nex-agi/nex-n2-pro")]
    )
    out = monitor.collapse_variants(batch)
    assert len(out) == 3  # two collapsed families + the singleton


def test_collapsed_entry_is_a_valid_modelrelease():
    # summarize_models / the template renderer consume ModelRelease unchanged,
    # so a collapsed family must BE a ModelRelease with a real name + url +
    # description, not a new type.
    six = [_m(f"allenai/qwen35-9b-{v}") for v in
           ["termigen", "cli-gym", "swesmith", "terminaltraj", "endless", "openthoughts"]]
    out = monitor.collapse_variants(six)
    entry = out[0]
    assert isinstance(entry, monitor.ModelRelease)
    assert entry.url.startswith("http")
    assert entry.name  # non-empty
    assert entry.description  # the family summary lives here


def test_empty_and_singletons_pass_through():
    assert monitor.collapse_variants([]) == []
    one = [_m("x/only-1b")]
    assert monitor.collapse_variants(one) == one


def test_collapsed_entry_carries_family_facts():
    # The family entry should carry the shared size (and org, implicitly via
    # name) so categorize_model and the renderer treat it as one release.
    six = [_m(f"allenai/qwen35-9b-{v}") for v in
           ["termigen", "cli-gym", "swesmith", "terminaltraj", "endless", "openthoughts"]]
    entry = monitor.collapse_variants(six)[0]
    # size is derivable from the family name (9b)
    assert monitor._param_size_from_name(entry.name) == "9B"
