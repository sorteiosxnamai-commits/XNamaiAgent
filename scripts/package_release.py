"""Build a clean release ZIP without credentials, caches, or Vercel metadata.

Fails closed only on findings classified as real secrets.
Never prints secret values — only path + variable + classification.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".vercel",
        ".pytest_cache",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "htmlcov",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "agent-transcripts",
    }
)

EXCLUDE_FILE_GLOBS = (
    "*.py[cod]",
    "*.zip",
    ".coverage",
    "coverage.xml",
    ".DS_Store",
    ".env",
    ".env.*",
)

ALLOW_ENV_EXAMPLE = ".env.example"

# --- Release policy -------------------------------------------------------
# The package is an ALLOWLIST of top-level entries: anything new at the repo
# root (local tool notes, editor config, debug dumps, scratch files) stays out
# until someone adds it here on purpose. Inside the allowed entries the
# exclusions below still apply.
RELEASE_TOP_LEVEL_DIRS = frozenset(
    {"api", "app", "scripts", "sql", "docs", "tests", ".github"}
)
RELEASE_TOP_LEVEL_FILES = frozenset(
    {
        "README.md",
        "requirements.txt",
        "requirements-dev.txt",
        "vercel.json",
        "pytest.ini",
        "persona_xnamai.txt",
        ALLOW_ENV_EXAMPLE,
        ".gitignore",
    }
)
RELEASE_EXCLUDE_DIR_NAMES = EXCLUDE_DIR_NAMES | frozenset(
    {
        ".superpowers",
        ".remember",
        ".cursor",
        ".idea",
        ".vscode",
        "coverage",
        "logs",
        "tmp",
        "temp",
    }
)
RELEASE_EXCLUDE_FILE_GLOBS = EXCLUDE_FILE_GLOBS + (
    # logs, debug dumps and scratch files
    "*.log",
    "debug-*",
    "*.tmp",
    "*.temp",
    "*.swp",
    "*.swo",
    "*~",
    "*.bak",
    "*.orig",
    ".coverage.*",
    # local credentials
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials*.json",
    "service-account*.json",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
)

SECRET_VAR_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "VERCEL_OIDC_TOKEN",
    "DATABASE_URL",
    "ADMIN_API_TOKEN",
    "BREVO_API_KEY",
    "CRON_SECRET",
)

# Same-line assignment only — never let whitespace eat newlines into the next key.
_ASSIGNMENT_RE = re.compile(
    r"(?m)^\s*(?P<var>"
    + "|".join(re.escape(n) for n in SECRET_VAR_NAMES)
    + r")[ \t]*=[ \t]*(?P<value>[^\n#]*?)[ \t]*(?:#.*)?$"
)

_PLACEHOLDER_EXACT = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "example",
        "test",
        "test-token",
        "test_token",
        "your-key-here",
        "your_key_here",
        "replace-me",
        "replace_me",
        "localhost",
        "none",
        "null",
        "todo",
        "xxx",
        "<token>",
        "<secret>",
        "sk-...",
        "Bearer test",
    }
)

_PLACEHOLDER_SUBSTRINGS = (
    "changeme",
    "your-key",
    "your_key",
    "replace-me",
    "replace_me",
    "dummy",
    "fake",
    "placeholder",
    "sample-token",
    "sample_secret",
)

_TEST_FIXTURE_VALUES = frozenset(
    {
        "tok-a",
        "tok-b",
        "tok-only",
        "test-token",
        "test_token",
        "token-test",
        "sk-test",
        "sk-test-key",
    }
)


class SecretFinding(BaseModel):
    path: str
    variable: str
    classification: Literal["real", "placeholder", "test_fixture"]
    blocking: bool = Field(description="True only for real secrets")


def _is_excluded_file(rel: Path, globs: tuple[str, ...] = EXCLUDE_FILE_GLOBS) -> bool:
    name = rel.name
    if name == ALLOW_ENV_EXAMPLE:
        return False
    for pattern in globs:
        if fnmatch.fnmatch(name, pattern):
            return True
    if name.startswith(".env"):
        return True
    return False


def _in_release_allowlist(rel: Path) -> bool:
    top = rel.parts[0]
    if len(rel.parts) == 1:
        return top in RELEASE_TOP_LEVEL_FILES
    return top in RELEASE_TOP_LEVEL_DIRS


def iter_repository_files(root: Path) -> list[Path]:
    """Every file worth scanning for secrets: caches and local ``.env*`` aside.

    Deliberately BROADER than the release: a secret in a local note or a log is
    still a leak, so ``scan_secrets.py`` uses this, not ``iter_release_files``.
    """
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if _is_excluded_file(rel):
            continue
        selected.append(path)
    return sorted(selected)


def iter_release_files(root: Path) -> list[Path]:
    """Files that go into the release ZIP: allowlist first, exclusions inside."""
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if not _in_release_allowlist(rel):
            continue
        if any(part in RELEASE_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if _is_excluded_file(rel, RELEASE_EXCLUDE_FILE_GLOBS):
            continue
        selected.append(path)
    return sorted(selected)


def skipped_top_level_entries(root: Path) -> list[str]:
    """Top-level names left out by the allowlist (names only, never contents)."""
    return sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in root.iterdir()
        if not (
            (entry.is_dir() and entry.name in RELEASE_TOP_LEVEL_DIRS)
            or (entry.is_file() and entry.name in RELEASE_TOP_LEVEL_FILES)
        )
    )


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def classify_secret_value(
    *,
    variable: str,
    value: str,
    path: str,
) -> SecretFinding:
    cleaned = _strip_quotes(value)
    folded = cleaned.casefold()
    rel = path.replace("\\", "/")
    in_tests = "/tests/" in f"/{rel}" or rel.startswith("tests/")

    if not cleaned or (cleaned.startswith("${") and cleaned.endswith("}")):
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="placeholder",
            blocking=False,
        )
    if folded in _PLACEHOLDER_EXACT:
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="placeholder",
            blocking=False,
        )
    if any(folded == s or folded.startswith(s + "-") or folded.startswith(s + "_") for s in _PLACEHOLDER_SUBSTRINGS):
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="placeholder",
            blocking=False,
        )
    if folded in {"example", "test", "localhost"}:
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="placeholder",
            blocking=False,
        )
    if folded.startswith("postgres://localhost") or folded.startswith("postgresql://localhost"):
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="placeholder",
            blocking=False,
        )
    if in_tests or folded in _TEST_FIXTURE_VALUES or folded.startswith("tok-"):
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="test_fixture",
            blocking=False,
        )
    # Heuristic real secrets: long opaque tokens / URLs with credentials.
    if (
        len(cleaned) >= 12
        or folded.startswith("sk-")
        or "://" in cleaned
        or variable == "VERCEL_OIDC_TOKEN"
    ):
        return SecretFinding(
            path=rel,
            variable=variable,
            classification="real",
            blocking=True,
        )
    return SecretFinding(
        path=rel,
        variable=variable,
        classification="real",
        blocking=True,
    )


def scan_file_for_secrets(path: Path, *, root: Path) -> list[tuple[str, str]]:
    """Backward-compatible: return only blocking (path, variable) pairs."""
    findings = classify_file_secrets(path, root=root)
    return [(f.path, f.variable) for f in findings if f.blocking]


def classify_file_secrets(path: Path, *, root: Path) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    rel = str(path.relative_to(root)).replace("\\", "/")
    if "\x00" in text[:2048]:
        return findings
    for match in _ASSIGNMENT_RE.finditer(text):
        findings.append(
            classify_secret_value(
                variable=match.group("var"),
                value=match.group("value") or "",
                path=rel,
            )
        )
    return findings


def validate_no_secrets(files: list[Path], *, root: Path) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for path in files:
        findings.extend(scan_file_for_secrets(path, root=root))
    return findings


def classify_package_secrets(files: list[Path], *, root: Path) -> list[SecretFinding]:
    out: list[SecretFinding] = []
    for path in files:
        out.extend(classify_file_secrets(path, root=root))
    return out


def build_zip(
    *,
    root: Path | None = None,
    output: Path | None = None,
    dry_run: bool = False,
) -> Path:
    base = root or REPO_ROOT
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = output or (base / "dist" / f"XNamaiAgent-release-{stamp}.zip")
    files = iter_release_files(base)
    classified = classify_package_secrets(files, root=base)
    blocking = [f for f in classified if f.blocking]
    if blocking:
        print("ERROR: real secret-like assignments found in package candidates:", file=sys.stderr)
        for finding in blocking:
            print(
                f"  path={finding.path} variable={finding.variable} "
                f"classification={finding.classification}",
                file=sys.stderr,
            )
        print(
            "Refuse to package. Remove secrets from tracked files "
            "(never commit .env / .env.local / OIDC tokens).",
            file=sys.stderr,
        )
        raise SystemExit(2)

    non_blocking = [f for f in classified if not f.blocking]
    if dry_run and non_blocking:
        print(
            f"info: {len(non_blocking)} placeholder/test_fixture assignment(s) ignored "
            "(values never printed)"
        )

    if dry_run:
        skipped = skipped_top_level_entries(base)
        if skipped:
            print(f"info: outside release allowlist (not packaged): {', '.join(skipped)}")
        print(f"dry-run: would package {len(files)} files -> {out}")
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.relative_to(base).as_posix()
            zf.write(path, arcname)
    print(f"ok: wrote {out} ({len(files)} files)")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        build_zip(root=args.root, output=args.output, dry_run=args.dry_run)
    except SystemExit as exc:
        return int(exc.code or 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
