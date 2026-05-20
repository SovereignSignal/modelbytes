"""Guard that build_digest_message's ALSO TRACKED section shows up to 10 models."""
import monitor


def _other_model(i: int) -> monitor.ModelRelease:
    """Build a ModelRelease that lands in the 'other' tier."""
    return monitor.ModelRelease(
        name=f"unknown-org/throwaway-{i}",
        provider="unknown-org",
        source="huggingface",
        url=f"https://huggingface.co/unknown-org/throwaway-{i}",
        description="",
        likes=600,  # over the 'other' threshold in categorize_model
    )


def test_other_section_shows_at_least_eight():
    """Given 12 'other' models, the digest should render 8+ of them, not silently drop them."""
    models = [_other_model(i) for i in range(12)]
    message = monitor.build_digest_message(models)
    rendered = sum(
        1
        for i in range(12)
        if f"throwaway-{i}" in message
    )
    assert rendered >= 8, (
        f"ALSO TRACKED section dropped {12 - rendered} of 12 models — "
        f"only {rendered} rendered."
    )
    assert "…and 2 more" in message, (
        "Overflow line '…and N more' missing — the new cap-overflow message did not render."
    )
