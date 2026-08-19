"""Tests for scripts/package_release.py — never assert secret values."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.package_release import (
    build_zip,
    iter_release_files,
    scan_file_for_secrets,
    validate_no_secrets,
)


def test_excludes_env_local_and_caches(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("OPENAI_API_KEY=should-not-pack\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "v.txt").write_text("x", encoding="utf-8")
    pycache = tmp_path / "app" / "__pycache__"
    pycache.mkdir()
    (pycache / "ok.cpython-312.pyc").write_bytes(b"\x00\x01")
    vercel = tmp_path / ".vercel"
    vercel.mkdir()
    (vercel / "project.json").write_text("{}", encoding="utf-8")

    files = {p.relative_to(tmp_path).as_posix() for p in iter_release_files(tmp_path)}
    assert "app/ok.py" in files
    assert ".env.example" in files
    assert ".env.local" not in files
    assert ".pytest_cache/v.txt" not in files
    assert "app/__pycache__/ok.cpython-312.pyc" not in files
    assert ".vercel/project.json" not in files


def test_scan_reports_variable_name_not_value(tmp_path: Path):
    secret = tmp_path / "leak.txt"
    secret.write_text("OPENAI_API_KEY=sk-secret-value-here-long-enough\n", encoding="utf-8")
    hits = scan_file_for_secrets(secret, root=tmp_path)
    assert hits == [("leak.txt", "OPENAI_API_KEY")]
    # Ensure we never surface the value in the hit tuple
    assert all("sk-secret" not in part for hit in hits for part in hit)


def test_env_example_empty_assignments_are_non_blocking(tmp_path: Path):
    from scripts.package_release import classify_file_secrets

    env = tmp_path / ".env.example"
    env.write_text("OPENAI_API_KEY=\nDATABASE_URL=\nADMIN_API_TOKEN=\n", encoding="utf-8")
    findings = classify_file_secrets(env, root=tmp_path)
    assert findings
    assert all(not f.blocking for f in findings)
    assert all(f.classification == "placeholder" for f in findings)


def test_build_zip_fails_on_secret_without_printing_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text(
        "DATABASE_URL=postgres://user:pass@db.example/prod\n", encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("ok\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        build_zip(root=tmp_path, output=tmp_path / "out.zip")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err
    assert "docs/note.md" in err.replace("\\", "/")
    assert "postgres://" not in err
    assert not (tmp_path / "out.zip").exists()


def test_build_zip_excludes_forbidden_paths(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("VERCEL_OIDC_TOKEN=should-never-pack\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("ADMIN_API_TOKEN=nope\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x\n", encoding="utf-8")
    (tmp_path / ".vercel").mkdir()
    (tmp_path / ".vercel" / "project.json").write_text("{}\n", encoding="utf-8")
    out = tmp_path / "release.zip"
    path = build_zip(root=tmp_path, output=out)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    forbidden_prefixes = (
        ".env.local",
        ".env.production",
        ".git/",
        ".vercel/",
        "__pycache__/",
        ".pytest_cache/",
    )
    for name in names:
        assert not name.startswith(forbidden_prefixes), name
        assert not name.endswith(".pyc")
        assert "VERCEL_OIDC_TOKEN" not in name
    assert "app/a.py" in names
    assert ".env.example" in names


def test_validate_no_secrets_clean(tmp_path: Path):
    f = tmp_path / "readme.md"
    f.write_text("Set OPENAI_API_KEY in Vercel only.\n", encoding="utf-8")
    assert validate_no_secrets([f], root=tmp_path) == []
