"""Tests for ``postcheck config`` subcommands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import postcheck.cli.commands.config as config_module
import postcheck.cli.commands.init as init_module
import postcheck.core.config as core_config
from postcheck.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Pin global config + cwd to an isolated dir per test."""
    global_path = tmp_path / "globalcfg" / "postcheck" / "config.json"
    # Patch the resolver in all modules that imported it by name.
    monkeypatch.setattr(
        core_config, "get_global_config_path", lambda: global_path
    )
    monkeypatch.setattr(
        config_module, "get_global_config_path", lambda: global_path
    )
    monkeypatch.setattr(
        init_module, "get_global_config_path", lambda: global_path
    )
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    # Clean env so tests are deterministic.
    for var in (
        "POSTCHECK_ADAPTER",
        "POSTCHECK_BASE_URL",
        "POSTCHECK_TIMEOUT_MS",
        "POSTCHECK_LAUNCH_MODE",
        "POSTCHECK_NETWORK__TREAT_CROSS_ORIGIN_AS",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield {"global_path": global_path, "project": project}


def _init_project(project: Path) -> None:
    """Run `postcheck init --yes` for a project."""
    result = runner.invoke(app, ["init", str(project), "--yes"])
    assert result.exit_code == 0, result.output


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_effective_returns_defaults_when_no_files(_isolated_paths):
    result = runner.invoke(app, ["config", "get"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # Built-in defaults.
    assert data["adapter"] == "auto"
    assert data["launch_mode"] == "attach"
    assert data["timeout_ms"] == 30_000


def test_get_show_attributes_correctly_across_layers(_isolated_paths):
    project = _isolated_paths["project"]
    global_path = _isolated_paths["global_path"]
    _init_project(project)

    # Set global launch_mode, project base_url.
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(json.dumps({"launch_mode": "launch"}), encoding="utf-8")
    proj_cfg = project / ".postcheck" / "config.json"
    proj_cfg.write_text(
        json.dumps({"base_url": "http://localhost:5173"}), encoding="utf-8"
    )

    result = runner.invoke(app, ["config", "get", "--show"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "launch_mode" in out
    assert "global" in out
    assert "base_url" in out
    assert "project" in out
    # timeout_ms still default
    assert "timeout_ms" in out
    assert "default" in out


def test_get_single_key(_isolated_paths):
    result = runner.invoke(app, ["config", "get", "launch_mode"])
    assert result.exit_code == 0
    assert result.output.strip() == "attach"


def test_get_single_key_with_show_includes_provenance(_isolated_paths):
    result = runner.invoke(app, ["config", "get", "launch_mode", "--show"])
    assert result.exit_code == 0
    assert "launch_mode" in result.output
    assert "default" in result.output


def test_get_unknown_key_suggests(_isolated_paths):
    result = runner.invoke(app, ["config", "get", "launchmode"])
    assert result.exit_code == 3, result.output
    assert "unknown key" in result.output.lower()
    assert "launch_mode" in result.output.lower()


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def test_set_project_field_writes_to_project_file(_isolated_paths):
    project = _isolated_paths["project"]
    _init_project(project)
    proj_cfg = project / ".postcheck" / "config.json"

    result = runner.invoke(
        app, ["config", "set", "base_url", "http://localhost:3001"]
    )
    assert result.exit_code == 0, result.output
    data = _read_json(proj_cfg)
    assert data["base_url"] == "http://localhost:3001"
    assert "global" not in result.output.split("\n")[0].lower() or "project" in result.output.lower()


def test_set_global_field_writes_to_global_file(_isolated_paths):
    global_path = _isolated_paths["global_path"]
    result = runner.invoke(
        app, ["config", "set", "launch_mode", "launch", "--global"]
    )
    assert result.exit_code == 0, result.output
    data = _read_json(global_path)
    assert data["launch_mode"] == "launch"


def test_set_project_only_field_with_global_flag_rejected(_isolated_paths):
    # base_url is project-only
    result = runner.invoke(
        app, ["config", "set", "base_url", "http://x", "--global"]
    )
    assert result.exit_code == 3
    assert "cannot be set at the global layer" in result.output.lower()


def test_set_global_only_field_without_global_rejected(_isolated_paths):
    project = _isolated_paths["project"]
    _init_project(project)
    result = runner.invoke(app, ["config", "set", "launch_mode", "launch"])
    assert result.exit_code == 3
    assert "cannot be set at the project layer" in result.output.lower()


def test_set_unknown_key_suggests_close_match(_isolated_paths):
    project = _isolated_paths["project"]
    _init_project(project)
    result = runner.invoke(app, ["config", "set", "base_ur1", "http://x"])
    assert result.exit_code == 3
    assert "unknown key" in result.output.lower()
    assert "base_url" in result.output


def test_set_invalid_value_type_rejected_without_writing(_isolated_paths):
    global_path = _isolated_paths["global_path"]
    result = runner.invoke(
        app,
        ["config", "set", "chrome_debug_port", "not-an-int", "--global"],
    )
    assert result.exit_code == 3
    assert "integer" in result.output.lower()
    assert not global_path.exists()


def test_nested_key_dot_notation_works(_isolated_paths):
    project = _isolated_paths["project"]
    _init_project(project)
    proj_cfg = project / ".postcheck" / "config.json"

    result = runner.invoke(
        app,
        ["config", "set", "network.treat_cross_origin_as", "low"],
    )
    assert result.exit_code == 0, result.output
    data = _read_json(proj_cfg)
    assert data["network"]["treat_cross_origin_as"] == "low"


def test_list_value_via_json_string_works(_isolated_paths):
    project = _isolated_paths["project"]
    _init_project(project)
    proj_cfg = project / ".postcheck" / "config.json"

    result = runner.invoke(
        app,
        ["config", "set", "network.ignore_patterns", '["**/*.hot-update.*"]'],
    )
    assert result.exit_code == 0, result.output
    data = _read_json(proj_cfg)
    assert data["network"]["ignore_patterns"] == ["**/*.hot-update.*"]


# ---------------------------------------------------------------------------
# unset
# ---------------------------------------------------------------------------


def test_unset_falls_back_to_lower_layer(_isolated_paths):
    project = _isolated_paths["project"]
    global_path = _isolated_paths["global_path"]
    _init_project(project)

    # global sets launch_headless False; project overrides to True. Unset project
    # → should resolve back to global.
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        json.dumps({"launch_headless": False}), encoding="utf-8"
    )
    proj_cfg = project / ".postcheck" / "config.json"
    proj_cfg.write_text(
        json.dumps({"launch_headless": True}), encoding="utf-8"
    )

    result = runner.invoke(app, ["config", "unset", "launch_headless"])
    assert result.exit_code == 0, result.output
    assert "global" in result.output
    assert "False" in result.output


def test_unset_idempotent_when_key_absent(_isolated_paths):
    project = _isolated_paths["project"]
    _init_project(project)
    result = runner.invoke(app, ["config", "unset", "base_url"])
    # base_url wasn't written (matches global default? in init we may have written it).
    # Either way: should not crash.
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# init interaction
# ---------------------------------------------------------------------------


def test_init_skips_globally_set_fields(_isolated_paths, tmp_path):
    """If global config already has a value, init shouldn't duplicate it."""
    global_path = _isolated_paths["global_path"]
    # Set base_url at global to the same value the user is about to choose.
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(
        json.dumps({}), encoding="utf-8"
    )
    # If global says base_url already, init shouldn't re-write it.
    # We can't trivially make base_url global (it's project-only), but
    # default_adapter_override is global. Pre-set it; init shouldn't write
    # adapter to project if detection agrees.
    global_path.write_text(
        json.dumps({"default_adapter_override": "plain_html"}),
        encoding="utf-8",
    )

    proj = tmp_path / "html_proj"
    proj.mkdir()
    (proj / "index.html").write_text("<html></html>", encoding="utf-8")

    result = runner.invoke(app, ["init", str(proj), "--yes"])
    assert result.exit_code == 0, result.output
    written = _read_json(proj / ".postcheck" / "config.json")
    # Adapter shouldn't be written because global already covers it.
    assert "adapter" not in written
