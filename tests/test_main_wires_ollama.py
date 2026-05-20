"""Guard that main() wires fetch_ollama_models into the fetcher loop."""
import ast
from pathlib import Path

MONITOR = Path(__file__).resolve().parent.parent / "monitor.py"


def test_main_includes_ollama_fetcher():
    """The fetcher list inside main() must reference fetch_ollama_models by name."""
    tree = ast.parse(MONITOR.read_text())
    main_fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    referenced = {
        node.id
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Name)
    }
    assert "fetch_ollama_models" in referenced, (
        "main() does not reference fetch_ollama_models — Ollama source is unwired."
    )
