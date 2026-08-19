#!/usr/bin/env python3
"""Scan the repo for credential files and known secret assignments.

Fails closed on real secrets. Never prints secret values.

- Forbidden paths: files **tracked by git** matching `.env*` / `.vercel`
  (local gitignored `.env.local` is allowed on developer machines).
- Assignments: same classifier as `package_release.py` over release files.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parents[0]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from package_release import (  # noqa: E402
    SecretFinding,
    _ASSIGNMENT_RE,
    classify_secret_value,
    iter_release_files,
)

FORBIDDEN_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.staging",
    }
)


def _git_tracked_files(root: Path) -> list[str] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    raw = completed.stdout.split(b"\0")
    out: list[str] = []
    for item in raw:
        if not item:
            continue
        out.append(item.decode("utf-8", errors="replace").replace("\\", "/"))
    return out


def scan_forbidden_tracked_paths(root: Path) -> list[str]:
    tracked = _git_tracked_files(root)
    if tracked is None:
        # Not a git checkout — fall back to release file set only (no .env*).
        return []
    hits: list[str] = []
    for rel in tracked:
        name = Path(rel).name
        parts = Path(rel).parts
        if ".vercel" in parts:
            hits.append(rel)
            continue
        if name in FORBIDDEN_BASENAMES or (
            name.startswith(".env") and name != ".env.example"
        ):
            hits.append(rel)
    return hits


def scan_secret_assignments(root: Path) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in iter_release_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        for match in _ASSIGNMENT_RE.finditer(text):
            findings.append(
                classify_secret_value(
                    variable=match.group("var"),
                    value=match.group("value"),
                    path=rel,
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to scan",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    forbidden = scan_forbidden_tracked_paths(root)
    findings = scan_secret_assignments(root)
    blocking = [f for f in findings if f.blocking]

    if forbidden:
        print("scan_secrets: forbidden credential paths tracked by git:")
        for item in forbidden:
            print(f"  - {item}")

    if blocking:
        print("scan_secrets: blocking secret assignments:")
        for item in blocking:
            print(f"  - {item.path} :: {item.variable} ({item.classification})")

    placeholders = [f for f in findings if not f.blocking]
    if placeholders:
        print(f"scan_secrets: non-blocking placeholders/fixtures: {len(placeholders)}")

    if forbidden or blocking:
        print("scan_secrets: FAILED")
        return 1

    print("scan_secrets: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
