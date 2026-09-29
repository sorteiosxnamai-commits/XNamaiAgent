"""Nenhum caractere de controle invisivel no codigo-fonte.

Motivo: ``tests/test_prompt_surface_regression.py`` tinha um BACKSPACE (0x08)
literal onde deveria haver ``\\b`` em tres regex. Os padroes nunca casavam e a
guarda de interpolacao vazia passava a vacuo. O mesmo defeito reapareceu ao
editar ``_has_legacy_store_identity``.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("app", "api", "scripts", "tests", "sql")
SOURCE_SUFFIXES = {".py", ".sql", ".json", ".md", ".txt"}
ALLOWED_CONTROL = {"\n", "\r", "\t"}


def _source_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for base in SOURCE_DIRS:
        files.extend(
            path
            for path in sorted((REPO_ROOT / base).rglob("*"))
            if path.is_file()
            and path.suffix in SOURCE_SUFFIXES
            and "__pycache__" not in path.parts
        )
    return files


SOURCE_FILES = _source_files()


def test_scan_is_not_empty():
    assert len(SOURCE_FILES) > 200


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_no_invisible_control_characters(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    bad = sorted({hex(ord(ch)) for ch in text if ord(ch) < 32 and ch not in ALLOWED_CONTROL})
    assert not bad, f"{path.relative_to(REPO_ROOT)} contem caracteres de controle {bad}"
