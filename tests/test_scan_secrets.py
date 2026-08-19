"""Tests for scripts/scan_secrets.py (FASE 0)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_scan_secrets():
    import sys

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    path = SCRIPTS / "scan_secrets.py"
    spec = importlib.util.spec_from_file_location("scan_secrets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_secrets_ok_on_repo():
    mod = _load_scan_secrets()
    assert mod.main(["--root", str(ROOT)]) == 0


def test_scan_secrets_fails_when_env_local_is_tracked(tmp_path: Path):
    mod = _load_scan_secrets()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".env.local").write_text(
        "VERCEL_OIDC_TOKEN=real-looking-token-value-long\n",
        encoding="utf-8",
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-f", ".env.local", "app/ok.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert mod.main(["--root", str(tmp_path)]) == 1
