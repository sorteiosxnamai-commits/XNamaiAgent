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


def _touch(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_release_is_an_allowlist_of_top_level_entries(tmp_path: Path):
    """Pasta ou arquivo novo na raiz fica FORA ate ser liberado de proposito."""
    for rel in (
        "app/a.py",
        "api/index.py",
        "sql/001.sql",
        "README.md",
        "requirements.txt",
        "vercel.json",
        ".env.example",
        # locais / sessao / editor / novidade desconhecida
        ".superpowers/sdd/report.md",
        ".remember/today.md",
        ".cursor/mcp.json",
        ".idea/workspace.xml",
        "notes-from-agent.md",
        "scratch/anything.py",
    ):
        _touch(tmp_path, rel)

    files = {p.relative_to(tmp_path).as_posix() for p in iter_release_files(tmp_path)}

    assert {"app/a.py", "api/index.py", "sql/001.sql", "README.md", "requirements.txt",
            "vercel.json", ".env.example"} <= files
    for leaked in (".superpowers/sdd/report.md", ".remember/today.md", ".cursor/mcp.json",
                   ".idea/workspace.xml", "notes-from-agent.md", "scratch/anything.py"):
        assert leaked not in files, leaked


@pytest.mark.parametrize(
    "rel",
    [
        "debug-38b290.log",
        "app/debug-session.json",
        "app/worker.log",
        "docs/trace.tmp",
        "app/module.py.bak",
        "app/.module.py.swp",
        "scripts/deploy.pem",
        "scripts/private.key",
        "app/credentials.json",
        "app/service-account-prod.json",
        "app/id_rsa",
        "app/local.sqlite3",
        ".coverage",
        ".coverage.worker1",
        "app/.env.production",
        "app/logs/today.txt",
        "app/tmp/partial.txt",
    ],
)
def test_release_excludes_logs_debug_temp_and_credentials(tmp_path: Path, rel: str):
    _touch(tmp_path, "app/a.py")
    _touch(tmp_path, rel)
    files = {p.relative_to(tmp_path).as_posix() for p in iter_release_files(tmp_path)}
    assert rel not in files
    assert "app/a.py" in files


def test_secret_scan_is_broader_than_the_release(tmp_path: Path):
    """Nota local fora do pacote continua sendo varrida por segredo."""
    from scripts.package_release import iter_repository_files
    from scripts.scan_secrets import scan_secret_assignments

    _touch(tmp_path, "app/a.py")
    _touch(tmp_path, ".superpowers/notes.md", "OPENAI_API_KEY=sk-real-looking-value-123456\n")

    released = {p.relative_to(tmp_path).as_posix() for p in iter_release_files(tmp_path)}
    scanned = {p.relative_to(tmp_path).as_posix() for p in iter_repository_files(tmp_path)}
    assert ".superpowers/notes.md" not in released
    assert ".superpowers/notes.md" in scanned
    blocking = [f for f in scan_secret_assignments(tmp_path) if f.blocking]
    assert [(f.path, f.variable) for f in blocking] == [(".superpowers/notes.md", "OPENAI_API_KEY")]


def test_real_repository_release_keeps_runtime_and_drops_local_state():
    root = Path(__file__).resolve().parents[1]
    files = {p.relative_to(root).as_posix() for p in iter_release_files(root)}
    for required in ("api/index.py", "app/config.py", "vercel.json", "requirements.txt", ".env.example"):
        assert required in files
    assert not any(
        name.startswith((".superpowers/", ".remember/", ".cursor/", ".git/")) or name.endswith(".log")
        or name == ".env"
        for name in files
    )
