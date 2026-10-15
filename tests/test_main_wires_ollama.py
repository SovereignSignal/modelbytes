"""Guard that main() wires fetch_ollama_models into the fetcher loop."""
import ast
from pathlib import Path

MONITOR = Path(__file__).resolve().parent.parent / "monitor.py"


def test_main_includes_ollama_fetcher():
    """The fetcher list inside main() must reference fetch_ollama_models by name."""
    tree = ast.parse(MONITOR.read_text())
    # main() is a thin wrapper; the fetcher loop lives in _run_digest.
    fns = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("main", "_run_digest")
    ]
    assert len(fns) == 2
    referenced = {
        node.id
        for fn in fns
        for node in ast.walk(fn)
        if isinstance(node, ast.Name)
    }
    assert "fetch_ollama_models" in referenced, (
        "main() does not reference fetch_ollama_models — Ollama source is unwired."
    )
