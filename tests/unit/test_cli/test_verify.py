"""Tests for ``postcheck verify`` — orchestrator is mocked."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from postcheck.cli.commands import verify as verify_cmd
from postcheck.cli.main import app
from postcheck.core.errors import PostcheckError
from postcheck.core.types import (
    AffectedRoute,
    Bug,
    BugLocation,
    FileChange,
    VerifyOptions,
    VerifyResult,
)
from postcheck.db.session import init_db

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """An initialised postcheck project root with the DB created."""
    asyncio.run(init_db(tmp_path))
    (tmp_path / ".postcheck" / "config.json").write_text(
        json.dumps({"launch_mode": "launch", "adapter": "auto"}),
        encoding="utf-8",
    )
    return tmp_path


def _make_result(
    *,
    status: str = "completed",
    bugs: list[Bug] | None = None,
    file_changes: list[FileChange] | None = None,
    affected: list[AffectedRoute] | None = None,
    error: str | None = None,
) -> VerifyResult:
    now = datetime.now(timezone.utc)
    return VerifyResult(
        status=status,  # type: ignore[arg-type]
        run_id="00000000-0000-0000-0000-000000000000",
        started_at=now,
        finished_at=now,
        file_changes=file_changes or [],
        symbol_changes=[],
        affected_routes=affected or [],
        events=[],
        bugs=bugs or [],
        report_paths={},
        error=error,
    )


def _patch_run_verification(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: VerifyResult | None = None,
    raise_exc: BaseException | None = None,
) -> dict[str, Any]:
    """Patch the orchestrator imported into the verify command."""
    captured: dict[str, Any] = {}

    async def fake(opts: VerifyOptions) -> VerifyResult:
        captured["opts"] = opts
        if raise_exc is not None:
            raise raise_exc
        assert result is not None
        return result

    monkeypatch.setattr(verify_cmd, "run_verification", fake)
    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_verify_no_project_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1, result.output
    assert "no postcheck project" in result.output.lower()


def test_verify_project_path_not_initialised_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["verify", "--project", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "is not a postcheck project" in result.output.lower()


def test_verify_clean_no_changes_exits_0(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    _patch_run_verification(monkeypatch, result=_make_result(file_changes=[]))

    result = runner.invoke(app, ["verify", "--since", "HEAD~5"])
    assert result.exit_code == 0, result.output
    assert "no changes since head~5" in result.output.lower()


def test_verify_no_affected_routes_exits_0(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    fc = FileChange(path=Path("src/foo.ts"), kind="modified")
    _patch_run_verification(
        monkeypatch, result=_make_result(file_changes=[fc], affected=[])
    )
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "no affected routes" in result.output.lower()


def test_verify_with_bugs_exits_1(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    bug = Bug(
        probe="runtime",
        route="/cart",
        severity="high",
        confidence="deterministic",
        title="TypeError: x is undefined",
        detail="thrown on click",
        suspected_location=BugLocation(file=Path("src/cart.ts"), line=42),
    )
    fc = FileChange(path=Path("src/cart.ts"), kind="modified")
    affected = AffectedRoute(
        route="/cart",
        reason="direct",
        confidence="high",
        changed_symbols=[],
        suspected_selectors=[],
    )
    _patch_run_verification(
        monkeypatch,
        result=_make_result(bugs=[bug], file_changes=[fc], affected=[affected]),
    )

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1, result.output
    assert "TypeError" in result.output
    assert "/cart" in result.output


def test_verify_postcheck_error_exits_2(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    _patch_run_verification(
        monkeypatch, raise_exc=PostcheckError("dev server unreachable")
    )
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output


def test_verify_unexpected_exception_exits_2(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    _patch_run_verification(monkeypatch, raise_exc=RuntimeError("boom"))
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output
    assert "boom" in result.output.lower() or "errored" in result.output.lower()


def test_verify_json_flag_emits_json(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    bug = Bug(
        probe="runtime",
        route="/",
        confidence="deterministic",
        title="boom",
    )
    fc = FileChange(path=Path("src/x.ts"), kind="modified")
    affected = AffectedRoute(
        route="/", reason="direct", confidence="high",
        changed_symbols=[], suspected_selectors=[],
    )
    _patch_run_verification(
        monkeypatch,
        result=_make_result(bugs=[bug], file_changes=[fc], affected=[affected]),
    )
    result = runner.invoke(app, ["verify", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output.split("postcheck verify")[0] or result.output)
    assert payload["summary"]["total"] == 1


def test_verify_output_flag_writes_markdown_and_json(
    project_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(project_root)
    fc = FileChange(path=Path("src/x.ts"), kind="modified")
    affected = AffectedRoute(
        route="/", reason="direct", confidence="high",
        changed_symbols=[], suspected_selectors=[],
    )
    bug = Bug(probe="runtime", route="/", confidence="deterministic", title="boom")
    _patch_run_verification(
        monkeypatch,
        result=_make_result(bugs=[bug], file_changes=[fc], affected=[affected]),
    )
    out_path = tmp_path / "report.md"
    result = runner.invoke(app, ["verify", "--output", str(out_path)])
    assert result.exit_code == 1, result.output
    assert out_path.is_file()
    assert "boom" in out_path.read_text()
    json_path = out_path.with_suffix(".md.json")
    assert json_path.is_file()
    payload = json.loads(json_path.read_text())
    assert payload["summary"]["total"] == 1


def test_verify_persists_run_row_and_bugs(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    bug = Bug(
        probe="runtime",
        route="/",
        confidence="deterministic",
        title="oops",
        suspected_location=BugLocation(file=Path("src/x.ts"), line=10),
    )
    fc = FileChange(path=Path("src/x.ts"), kind="modified")
    affected = AffectedRoute(
        route="/", reason="direct", confidence="high",
        changed_symbols=[], suspected_selectors=[],
    )
    _patch_run_verification(
        monkeypatch,
        result=_make_result(bugs=[bug], file_changes=[fc], affected=[affected]),
    )
    runner.invoke(app, ["verify"])

    db_path = project_root / ".postcheck" / "postcheck.db"
    conn = sqlite3.connect(db_path)
    try:
        runs = conn.execute(
            "SELECT status, total_bugs, since_ref FROM run"
        ).fetchall()
        assert runs == [("succeeded", 1, "HEAD~1")]
        bugs = conn.execute(
            "SELECT probe, route, suspected_file, suspected_line, confidence FROM bug"
        ).fetchall()
        assert bugs == [("runtime", "/", "src/x.ts", 10, "deterministic")]
    finally:
        conn.close()


def test_verify_since_flag_forwarded(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    captured = _patch_run_verification(
        monkeypatch, result=_make_result(file_changes=[])
    )
    runner.invoke(app, ["verify", "--since", "main"])
    assert captured["opts"].since == "main"


def test_verify_postcheck_error_persists_run_with_failed_status(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project_root)
    _patch_run_verification(
        monkeypatch, raise_exc=PostcheckError("dev server down")
    )
    runner.invoke(app, ["verify"])
    db_path = project_root / ".postcheck" / "postcheck.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT status, total_bugs FROM run").fetchall()
        assert rows == [("failed", 0)]
        report = conn.execute("SELECT report_json FROM run").fetchone()[0]
        payload = json.loads(report)
        assert "dev server down" in payload["error"]
    finally:
        conn.close()
