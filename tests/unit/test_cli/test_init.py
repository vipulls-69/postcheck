"""Tests for ``postcheck init``."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from postcheck.cli.main import app

runner = CliRunner()


def _scaffold_react_vite(root: Path) -> None:
    """Minimal React+Vite shape that ReactViteAdapter.detect() accepts."""
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


def test_init_in_empty_dir_succeeds(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--yes"])
    assert result.exit_code == 0, result.output

    postcheck_dir = tmp_path / ".postcheck"
    assert postcheck_dir.is_dir()
    config_path = postcheck_dir / "config.json"
    assert config_path.is_file()
    db_path = postcheck_dir / "postcheck.db"
    assert db_path.is_file()

    config = json.loads(config_path.read_text())
    assert config["launch_mode"] == "launch"
    assert config["launch_headless"] is True
    assert config["adapter"] == "auto"
    assert config["timeout_ms"] == 30_000
    assert config["base_url"] == "http://localhost:5173"

    # Default org + Project rows seeded.
    conn = sqlite3.connect(db_path)
    try:
        orgs = conn.execute("SELECT name, slug FROM organization").fetchall()
        assert orgs == [("Default", "default")]
        projects = conn.execute(
            "SELECT name, local_path, default_adapter FROM project"
        ).fetchall()
        assert len(projects) == 1
        name, local_path, adapter = projects[0]
        assert name == tmp_path.name
        assert Path(local_path) == tmp_path.resolve()
        # Empty dir: no adapter detected.
        assert adapter is None
    finally:
        conn.close()

    assert "postcheck initialised" in result.output
    assert "auto-detect at run time" in result.output


def test_init_already_initialised_fails_cleanly(tmp_path: Path) -> None:
    (tmp_path / ".postcheck").mkdir()

    result = runner.invoke(app, ["init", str(tmp_path), "--yes"])
    assert result.exit_code == 3, result.output
    assert "already initialised" in result.output.lower()


def test_init_yes_skips_prompts(tmp_path: Path) -> None:
    """With --yes, no stdin is consumed and defaults are used verbatim."""
    result = runner.invoke(app, ["init", str(tmp_path), "--yes"], input="")
    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".postcheck" / "config.json").read_text())
    assert config["base_url"] == "http://localhost:5173"


def test_init_prompts_for_base_url_without_yes(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["init", str(tmp_path)], input="http://localhost:1234\n"
    )
    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".postcheck" / "config.json").read_text())
    assert config["base_url"] == "http://localhost:1234"


def test_init_prompt_accepts_default_on_empty_input(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)], input="\n")
    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".postcheck" / "config.json").read_text())
    assert config["base_url"] == "http://localhost:5173"


def test_init_detects_react_vite_adapter(tmp_path: Path) -> None:
    _scaffold_react_vite(tmp_path)
    result = runner.invoke(app, ["init", str(tmp_path), "--yes"])
    assert result.exit_code == 0, result.output

    db_path = tmp_path / ".postcheck" / "postcheck.db"
    conn = sqlite3.connect(db_path)
    try:
        (adapter,) = conn.execute(
            "SELECT default_adapter FROM project"
        ).fetchone()
    finally:
        conn.close()
    assert adapter == "react_vite"
    assert "react_vite" in result.output


def test_init_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".postcheck" / "postcheck.db").is_file()


def test_init_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = runner.invoke(app, ["init", str(missing), "--yes"])
    assert result.exit_code == 3, result.output
    assert "does not exist" in result.output.lower()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "postcheck" in result.output.lower()
