from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from postcheck.core import orchestrator as orch
from postcheck.core.errors import AnalysisError, CDPAttachError
from postcheck.browser.target_locator import LocatedTarget
from postcheck.core.types import (
    AffectedRoute,
    LocationFailure,
    ProbeEvent,
    Selector,
    Symbol,
    VerifyOptions,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubPage:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _StubContext:
    def __init__(self) -> None:
        self.pages: list[_StubPage] = []

    async def new_page(self) -> _StubPage:
        p = _StubPage()
        self.pages.append(p)
        return p


class _StubSession:
    def __init__(self) -> None:
        self.context = _StubContext()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _git_init(root: Path) -> None:
    subprocess.check_call(["git", "init", "-q", str(root)])
    subprocess.check_call(
        ["git", "-C", str(root), "config", "user.email", "t@t"]
    )
    subprocess.check_call(
        ["git", "-C", str(root), "config", "user.name", "t"]
    )
    subprocess.check_call(
        ["git", "-C", str(root), "config", "commit.gpgsign", "false"]
    )


def _commit(root: Path, msg: str = "init") -> None:
    subprocess.check_call(["git", "-C", str(root), "add", "-A"])
    subprocess.check_call(
        ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", msg]
    )


# ---------------------------------------------------------------------------
# Pre-attach paths (no browser)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_clean_when_no_files_changed(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.txt").write_text("hi")
    _commit(tmp_path)

    opts = VerifyOptions(project_root=tmp_path, since="HEAD")
    result = await orch.run_verification(opts)

    assert result.status == "completed"
    assert result.bugs == []
    assert result.file_changes == []
    assert result.affected_routes == []
    assert result.run_id  # non-empty
    assert result.finished_at is not None
    assert result.report_paths  # write_report still runs for clean runs


@pytest.mark.asyncio
async def test_failed_status_when_not_a_git_repo(tmp_path: Path) -> None:
    opts = VerifyOptions(project_root=tmp_path)
    result = await orch.run_verification(opts)
    assert result.status == "failed"
    assert result.error is not None
    assert "AnalysisError" in result.error or "Analysis" in result.error
    assert result.bugs == []


@pytest.mark.asyncio
async def test_returns_clean_when_no_routes_affected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.html").write_text("<html></html>")
    _commit(tmp_path)
    # Make a real diff so we get past the early-out.
    (tmp_path / "x.html").write_text("<html><body>hi</body></html>")

    async def _no_routes(*_a, **_kw):
        return []

    monkeypatch.setattr(orch, "map_impact", _no_routes)

    opts = VerifyOptions(project_root=tmp_path, since="HEAD")
    result = await orch.run_verification(opts)
    assert result.status == "completed"
    assert result.affected_routes == []
    assert result.file_changes  # diff was non-empty
    assert result.bugs == []


@pytest.mark.asyncio
async def test_dry_run_skips_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.html").write_text("<html></html>")
    _commit(tmp_path)
    (tmp_path / "x.html").write_text("<html><body>hi</body></html>")

    affected = [
        AffectedRoute(
            route="/x", reason="direct", confidence="high"
        )
    ]

    async def _routes(*_a, **_kw):
        return affected

    monkeypatch.setattr(orch, "map_impact", _routes)

    called = {"attach": False}

    async def _attach(_cfg):  # pragma: no cover - should not run
        called["attach"] = True
        raise AssertionError("attach should not be called in dry_run")

    monkeypatch.setattr(orch, "cdp_attach", _attach)

    opts = VerifyOptions(project_root=tmp_path, since="HEAD", dry_run=True)
    result = await orch.run_verification(opts)
    assert result.status == "completed"
    assert result.affected_routes == affected
    assert called["attach"] is False


# ---------------------------------------------------------------------------
# Browser path (stubbed)
# ---------------------------------------------------------------------------


def _patch_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    affected: list[AffectedRoute],
    locate_result,
    scenario_events: list[ProbeEvent],
    scenario_exc: BaseException | None = None,
    attach_exc: BaseException | None = None,
) -> _StubSession:
    async def _routes(*_a, **_kw):
        return affected

    monkeypatch.setattr(orch, "map_impact", _routes)

    session = _StubSession()

    async def _attach(_cfg):
        if attach_exc is not None:
            raise attach_exc
        return session

    monkeypatch.setattr(orch, "cdp_attach", _attach)

    async def _locate(_page, _route):
        return locate_result

    monkeypatch.setattr(orch, "locate", _locate)

    async def _run_scenario(_page, _targets, _probes, *, config):
        if scenario_exc is not None:
            raise scenario_exc
        return list(scenario_events)

    monkeypatch.setattr(orch, "run_scenario", _run_scenario)
    return session


def _make_locate(targets: list[LocatedTarget], failures: list[LocationFailure]):
    # Build a minimal LocateResult-like object.
    class _R:
        def __init__(self) -> None:
            self.targets = targets
            self.failures = failures

    return _R()


@pytest.mark.asyncio
async def test_full_run_attaches_runs_route_and_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.html").write_text("<html></html>")
    _commit(tmp_path)
    (tmp_path / "x.html").write_text("<html><body>hi</body></html>")

    sym = Symbol(name="onclick", kind="function", file=Path("x.html"), start_line=1, end_line=1)
    selector = Selector(strategy="css", value="#btn")
    affected = [
        AffectedRoute(
            route="/x",
            reason="direct",
            confidence="high",
            changed_symbols=[sym],
            suspected_selectors=[selector],
        )
    ]
    target = LocatedTarget(selector=selector, locator=object())  # type: ignore[arg-type]
    locate_result = _make_locate([target], [])
    events = [
        ProbeEvent(
            probe="runtime",
            route="/x",
            payload={"type": "runtime_error", "message": "boom"},
        )
    ]

    session = _patch_full_pipeline(
        monkeypatch,
        affected=affected,
        locate_result=locate_result,
        scenario_events=events,
    )

    opts = VerifyOptions(project_root=tmp_path, since="HEAD")
    result = await orch.run_verification(opts)

    assert result.status == "completed"
    assert result.affected_routes == affected
    assert len(result.events) == 1
    assert any(b.probe == "runtime" for b in result.bugs)
    assert session.closed is True
    assert all(p.closed for p in session.context.pages)
    assert result.report_paths.get("markdown")


@pytest.mark.asyncio
async def test_cdp_attach_error_bubbles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.html").write_text("<html></html>")
    _commit(tmp_path)
    (tmp_path / "x.html").write_text("<html><body>hi</body></html>")

    affected = [AffectedRoute(route="/x", reason="direct", confidence="high")]
    _patch_full_pipeline(
        monkeypatch,
        affected=affected,
        locate_result=_make_locate([], []),
        scenario_events=[],
        attach_exc=CDPAttachError(port=9222, endpoint="http://localhost:9222"),
    )

    opts = VerifyOptions(project_root=tmp_path, since="HEAD")
    with pytest.raises(CDPAttachError):
        await orch.run_verification(opts)


@pytest.mark.asyncio
async def test_scenario_exception_becomes_orchestrator_bug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.html").write_text("<html></html>")
    _commit(tmp_path)
    (tmp_path / "x.html").write_text("<html><body>hi</body></html>")

    sym = Symbol(name="x", kind="function", file=Path("x.html"), start_line=1, end_line=1)
    selector = Selector(strategy="css", value="#btn")
    affected = [
        AffectedRoute(
            route="/x",
            reason="direct",
            confidence="high",
            suspected_selectors=[selector],
        )
    ]
    target = LocatedTarget(selector=selector, locator=object())  # type: ignore[arg-type]

    session = _patch_full_pipeline(
        monkeypatch,
        affected=affected,
        locate_result=_make_locate([target], []),
        scenario_events=[],
        scenario_exc=RuntimeError("kaboom"),
    )

    opts = VerifyOptions(project_root=tmp_path, since="HEAD")
    result = await orch.run_verification(opts)

    assert result.status == "completed"
    bugs = [b for b in result.bugs if b.probe == "scenario"]
    assert len(bugs) == 1
    assert "kaboom" in bugs[0].detail
    assert bugs[0].evidence["phase"] == "scenario"
    assert session.closed is True


@pytest.mark.asyncio
async def test_location_failure_becomes_bug_and_skips_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    (tmp_path / "x.html").write_text("<html></html>")
    _commit(tmp_path)
    (tmp_path / "x.html").write_text("<html><body>hi</body></html>")

    affected = [AffectedRoute(route="/x", reason="direct", confidence="high")]
    failure = LocationFailure(
        route="/x",
        reason="no_selectors_provided",
        detail="adapter could not derive selectors",
    )

    ran: dict[str, bool] = {"scenario": False}

    async def _routes(*_a, **_kw):
        return affected

    monkeypatch.setattr(orch, "map_impact", _routes)

    session = _StubSession()

    async def _attach(_cfg):
        return session

    monkeypatch.setattr(orch, "cdp_attach", _attach)

    async def _locate(_page, _route):
        return _make_locate([], [failure])

    monkeypatch.setattr(orch, "locate", _locate)

    async def _run_scenario(*_a, **_kw):  # pragma: no cover - must not run
        ran["scenario"] = True
        return []

    monkeypatch.setattr(orch, "run_scenario", _run_scenario)

    opts = VerifyOptions(project_root=tmp_path, since="HEAD")
    result = await orch.run_verification(opts)

    assert ran["scenario"] is False
    ui_bugs = [b for b in result.bugs if b.probe == "ui"]
    assert len(ui_bugs) == 1
    assert "No DOM target" in ui_bugs[0].title


def test_join_url_handles_paths() -> None:
    assert orch._join_url("http://localhost:3000", "/x") == "http://localhost:3000/x"
    assert orch._join_url("http://localhost:3000/", "x") == "http://localhost:3000/x"
    assert orch._join_url("http://localhost:3000", "") == "http://localhost:3000"
    assert orch._join_url("http://localhost", "https://other.com/y") == "https://other.com/y"
