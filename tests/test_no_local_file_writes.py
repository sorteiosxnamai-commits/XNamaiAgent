"""O runtime nao grava arquivos locais: observabilidade vai para ``log_event``.

Motivo: ``app/channels/meta_instagram.py`` gravava ``debug-38b290.log`` no
diretorio de trabalho a cada evento ignorado — instrumentacao de uma sessao de
depuracao que foi para producao. Em serverless o disco e efemero/somente
leitura, e um arquivo desses acumula payload de cliente fora de qualquer
politica de retencao.

A varredura e por AST (comentarios e strings nao contam) e percorre o sistema de
arquivos, nao o git.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ("app", "api")

#: Unica excecao: arquivo temporario do SO, apagado no ``finally`` do mesmo
#: bloco, porque o SDK de transcricao exige um arquivo com extensao.
ALLOWED_TEMPFILE_MODULES = {"app/audio_service.py"}

_WRITE_MODES = ("w", "a", "x", "+")
_WRITE_METHODS = {"write_text", "write_bytes"}
_FORBIDDEN_CALLS = {"FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler", "basicConfig"}


def _runtime_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for base in RUNTIME_DIRS:
        files.extend(
            path
            for path in sorted((REPO_ROOT / base).rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return files


def _mode_argument(call: ast.Call, position: int) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return str(call.args[position].value)
    return None


def local_file_writes(source: str, *, allow_tempfile: bool = False) -> list[str]:
    offenses: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "open":
            # builtin open(path, mode) ou Path(...).open(mode)
            position = 0 if isinstance(func, ast.Attribute) else 1
            mode = _mode_argument(node, position) or "r"
            if any(flag in mode for flag in _WRITE_MODES):
                offenses.append(f"linha {node.lineno}: open(..., {mode!r})")
        elif name in _WRITE_METHODS:
            offenses.append(f"linha {node.lineno}: {name}()")
        elif name in _FORBIDDEN_CALLS:
            offenses.append(f"linha {node.lineno}: {name}()")
        elif name in {"NamedTemporaryFile", "mkstemp", "TemporaryFile"} and not allow_tempfile:
            offenses.append(f"linha {node.lineno}: {name}()")
    return offenses


RUNTIME_FILES = _runtime_files()


def test_scan_is_not_empty():
    assert len(RUNTIME_FILES) > 50
    assert REPO_ROOT / "app" / "channels" / "meta_instagram.py" in RUNTIME_FILES


@pytest.mark.parametrize(
    "sample",
    [
        'Path("debug.log").open("a", encoding="utf-8").write("x")',
        'open("out.txt", "w")',
        'open("out.txt", mode="ab")',
        'Path("x").write_text("y")',
        'logging.FileHandler("agent.log")',
        'tempfile.NamedTemporaryFile(delete=False)',
    ],
)
def test_detector_catches_local_writes(sample):
    assert local_file_writes(sample)


@pytest.mark.parametrize("sample", ['open("x", "rb")', 'Path("x").read_text()', '# open("x", "w")'])
def test_detector_ignores_reads_and_comments(sample):
    assert local_file_writes(sample) == []


@pytest.mark.parametrize("path", RUNTIME_FILES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_runtime_does_not_write_local_files(path):
    rel = path.relative_to(REPO_ROOT).as_posix()
    offenses = local_file_writes(
        path.read_text(encoding="utf-8"),
        allow_tempfile=rel in ALLOWED_TEMPFILE_MODULES,
    )
    assert not offenses, f"{rel} grava arquivo local: {offenses}"


def test_debug_session_artifact_is_gone():
    assert not (REPO_ROOT / "debug-38b290.log").exists()
    source = (REPO_ROOT / "app" / "channels" / "meta_instagram.py").read_text(encoding="utf-8")
    assert "_agent_debug_log" not in source
    assert "38b290" not in source


def test_skipped_meta_event_goes_to_structured_log(monkeypatch, tmp_path):
    """O sinal operacional continua existindo — via log_event, sem arquivo."""
    import app.channels.meta_instagram as meta

    monkeypatch.chdir(tmp_path)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(meta, "log_event", lambda name, payload=None: events.append((name, payload or {})))

    payload = {
        "object": "instagram",
        "entry": [{"id": "1", "messaging": [{"read": {"mid": "m"}, "sender": {"id": "2"}, "recipient": {"id": "1"}}]}],
    }
    meta.parse_meta_instagram_messaging(payload)

    skipped = [payload for name, payload in events if name == "meta.instagram.event_skipped"]
    assert skipped and skipped[0]["reason"]
    assert list(tmp_path.iterdir()) == []
