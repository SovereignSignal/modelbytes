"""Writer ITEM names that skip a word or add the vendor still find their row.

Oct 17 2026: "FLUX 3 Klein" (HF flux-3-image-klein) and "Reflection Beam"
(Artificial Analysis "Beam 1.0") were dropped with "no candidate".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor

TODAY = "2026-10-17"
DROP = "Dropped entry with no candidate"


def _model(name, **kw):
    fields = dict(
        provider=kw.pop("provider", "Test"),
        source=kw.pop("source", "huggingface"),
        url=kw.pop("url", "https://example.com/model"),
        description=kw.pop("description", "A capable model."),
        release_date=kw.pop("release_date", "2026-10-16"),
    )
    fields.update(kw)
    return monitor.ModelRelease(name=name, **fields)


def _klein_rows():
    return [
        _model("black-forest-labs/flux-3-image-klein-base",
               provider="Black Forest Labs"),
        _model("black-forest-labs/flux-3-image-klein",
               provider="Black Forest Labs"),
        _model("black-forest-labs/flux-3-image-klein-comfy",
               provider="Black Forest Labs"),
    ]


def _beam_row():
    return _model("Beam 1.0", provider="Reflection AI", creator="Reflection AI",
                  source="artificial-analysis")


def test_flux_klein_picks_plain_hf_repo_over_base_and_comfy():
    model = monitor._match_model("FLUX 3 Klein", _klein_rows())
    assert model is not None
    assert model.name == "black-forest-labs/flux-3-image-klein"


def test_flux_klein_matches_dotted_display_name():
    row = _model("FLUX.3 Image Klein", provider="fal", source="fal")
    assert monitor._match_model("FLUX 3 Klein", [row]) is row


def test_flux_klein_matches_on_fal_id_trait_only():
    row = _model("Black Forest Labs image launch", provider="fal", source="fal",
                 unique_traits=["fal-id:fal/blackforestlabs/flux-3/klein"])
    assert monitor._match_model("FLUX 3 Klein", [row]) is row


def test_flux_klein_matches_family_variants():
    row = _model("Black Forest Labs family", provider="Black Forest Labs",
                 variants=["black-forest-labs/flux-3-image-klein",
                           "black-forest-labs/flux-3-image-klein-base"])
    assert monitor._match_model("FLUX 3 Klein", [row]) is row


def test_reflection_beam_matches_aa_row_via_vendor():
    row = _beam_row()
    assert monitor._match_model("Reflection Beam", [row]) is row


def test_reflection_beam_hf_id_with_org_still_matches():
    row = _model("reflection/Beam-1.0-501B-A23B", provider="Reflection AI")
    assert monitor._match_model("Reflection Beam", [row]) is row


def test_hallucinated_names_do_not_match():
    rows = _klein_rows() + [_beam_row()]
    for name in ("FLUX 4 Klein", "FLUX 3 Pro", "Black Forest Nova 5",
                 "Reflection Prism", "Reflection Prism 2"):
        assert monitor._match_model(name, rows) is None, name


def test_vendor_alone_does_not_match():
    assert monitor._match_model("Reflection AI", [_beam_row()]) is None


def test_existing_exact_and_substring_matches_unchanged():
    rows = _klein_rows()
    assert monitor._match_model("flux-3-image-klein-comfy", rows) is rows[2]
    gemma = _model("google/gemma-5-27b-it", provider="Google")
    assert monitor._match_model("Gemma 5 27B", [gemma]) is gemma


def test_render_structured_keeps_flux_klein_and_drops_hallucination(capsys):
    summary = "\n".join([
        "TAKE: Black Forest Labs ships a small image model.",
        "ITEM: FLUX 3 Klein",
        "PROSE: A compact image generator for local use.",
        "ITEM: Reflection Beam",
        "PROSE: A large open MoE from Reflection AI.",
        "ITEM: Black Forest Nova 5",
        "PROSE: Does not exist.",
    ])
    take, items = monitor._parse_writer_blocks(summary)
    text, stale = monitor._render_structured(
        take, items, _klein_rows() + [_beam_row()], today=TODAY)
    err = capsys.readouterr().err
    assert f"{DROP}: FLUX 3 Klein" not in err
    assert f"{DROP}: Reflection Beam" not in err
    assert f"{DROP}: Black Forest Nova 5" in err
    assert "FLUX 3 Klein" in text
    assert "Reflection Beam" in text
    assert "Nova 5" not in text
    assert stale == []
