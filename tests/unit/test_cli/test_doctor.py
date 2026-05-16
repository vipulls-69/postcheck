"""Tests for ``postcheck doctor``."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from postcheck.cli.main import app

runner = CliRunner()


def _scaffold(root: Path) -> None:
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "dependencies": {"react": "^18.0.0", "react-dom": "^18.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (root / "vite.config.ts").write_text(
        "import { defineConfig } from 'vite';\nexport default defineConfig({});\n",
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "App.tsx").write_text(
        "export default function App() { return <div>hi</div>; }\n",
        encoding="utf-8",
    )


def test_doctor_runs_and_reports_checklist(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    runner.invoke(app, ["init", str(tmp_path), "--yes"])

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    # The dev server check will fail (no server running) and likely playwright too
    # in CI without browsers; just ensure a checklist printed and exit is 0 or 1.
    assert result.exit_code in (0, 1), result.output
    out = result.output
    assert "Python" in out
    assert ".postcheck/" in out
    assert "DB readable" in out
    assert "dev server reachable" in out


def test_doctor_without_project_reports_failure(tmp_path: Path) -> None:
    # cwd shouldn't matter; we pass an empty dir with no .postcheck/.
    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert ".postcheck/" in result.output
    # The DB / project_dir checks should be marked failing.
    assert "FAIL" in result.output or "✗" in result.output
