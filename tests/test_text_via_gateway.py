"""Phase 3: business modules must not call chat.completions.create directly."""

from pathlib import Path


def test_no_direct_chat_completions_create_outside_gateway():
    root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in {"openai_gateway.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "chat.completions.create" in text:
            offenders.append(str(path.relative_to(root.parent)))
    assert offenders == [], (
        "Text generation must go through generate_text_output / gateway; "
        f"found direct create in: {offenders}"
    )
