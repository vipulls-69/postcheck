"""Tests for ``postcheck.analysis.diff_analyzer``."""
from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from postcheck.analysis.diff_analyzer import diff_against
from postcheck.core.errors import AnalysisError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _commit_all(repo: Repo, message: str) -> str:
    repo.git.add("-A")
    commit = repo.index.commit(message)
    return commit.hexsha


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A real git repo with one initial commit. No-op for second baseline."""
    root = tmp_path / "proj"
    root.mkdir()
    repo = Repo.init(root, initial_branch="main")
    # Local identity so commits don't fail in CI containers
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@postcheck.local")
        cw.set_value("user", "name", "test")
    (root / "README.md").write_text("hello\n")
    (root / "src").mkdir()
    (root / "src" / "kept.ts").write_text("export const kept = 1;\n")
    _commit_all(repo, "init")
    return root


def _repo(root: Path) -> Repo:
    return Repo(root)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


async def test_no_changes_returns_empty(repo_root: Path):
    # HEAD~1 doesn't exist (only one commit), but with no changes since HEAD
    # the user can pass since=HEAD explicitly.
    changes = await diff_against(repo_root, since="HEAD")
    assert changes == []


async def test_detects_added_modified_deleted(repo_root: Path):
    repo = _repo(repo_root)
    # baseline second commit so HEAD~1 resolves
    (repo_root / "src" / "old.ts").write_text("export const old = 1;\n")
    _commit_all(repo, "baseline")

    # Now make changes against HEAD~1
    (repo_root / "src" / "added.ts").write_text("export const added = 1;\n")
    (repo_root / "src" / "kept.ts").write_text("export const kept = 2;\n")  # modified
    (repo_root / "src" / "old.ts").unlink()  # deleted
    _commit_all(repo, "changes")

    changes = await diff_against(repo_root)  # default HEAD~1
    by_path = {c.path.as_posix(): c for c in changes}
    assert by_path["src/added.ts"].kind == "added"
    assert by_path["src/kept.ts"].kind == "modified"
    assert by_path["src/old.ts"].kind == "deleted"


async def test_languages_inferred_from_suffix(repo_root: Path):
    repo = _repo(repo_root)
    _commit_all(repo, "baseline")
    (repo_root / "src" / "Comp.tsx").write_text("export const X = 1;\n")
    (repo_root / "src" / "style.css").write_text(".x { color: red; }\n")
    (repo_root / "src" / "page.html").write_text("<p>x</p>\n")
    _commit_all(repo, "polyglot")

    changes = await diff_against(repo_root)
    langs = {c.path.suffix: c.language for c in changes}
    assert langs[".tsx"] == "typescript"
    assert langs[".css"] == "css"
    assert langs[".html"] == "html"


async def test_hunks_parsed_with_unified_diff_offsets(repo_root: Path):
    repo = _repo(repo_root)
    (repo_root / "src" / "kept.ts").write_text("a\nb\nc\nd\ne\n")
    _commit_all(repo, "baseline")
    (repo_root / "src" / "kept.ts").write_text("a\nB\nc\nd\nE\n")
    _commit_all(repo, "edits")

    changes = await diff_against(repo_root)
    [change] = [c for c in changes if c.path.as_posix() == "src/kept.ts"]
    assert change.hunks, "expected at least one hunk"
    for h in change.hunks:
        assert h.old_start >= 1
        assert h.new_start >= 1
        assert h.content.startswith("@@")


async def test_lockfiles_and_build_dirs_filtered(repo_root: Path):
    repo = _repo(repo_root)
    _commit_all(repo, "baseline")
    (repo_root / "package-lock.json").write_text("{}\n")
    (repo_root / "pnpm-lock.yaml").write_text("lockfileVersion: 6\n")
    (repo_root / "yarn.lock").write_text("# yarn\n")
    (repo_root / "uv.lock").write_text("# uv\n")
    (repo_root / "Pipfile.lock").write_text("{}\n")
    (repo_root / "dist").mkdir()
    (repo_root / "dist" / "bundle.js").write_text("/*built*/\n")
    (repo_root / ".next").mkdir()
    (repo_root / ".next" / "manifest.json").write_text("{}\n")
    (repo_root / "src" / "real.ts").write_text("export const r = 1;\n")
    _commit_all(repo, "noise + signal")

    changes = await diff_against(repo_root)
    paths = {c.path.as_posix() for c in changes}
    assert paths == {"src/real.ts"}


async def test_custom_exclude_globs_respected(repo_root: Path):
    repo = _repo(repo_root)
    _commit_all(repo, "baseline")
    (repo_root / "src" / "secret.ts").write_text("export const s = 1;\n")
    (repo_root / "src" / "public.ts").write_text("export const p = 1;\n")
    _commit_all(repo, "changes")

    changes = await diff_against(repo_root, exclude_globs=["**/secret.ts"])
    paths = {c.path.as_posix() for c in changes}
    assert paths == {"src/public.ts"}


async def test_missing_since_ref_raises_analysis_error(repo_root: Path):
    with pytest.raises(AnalysisError) as exc:
        await diff_against(repo_root, since="nonexistent-ref")
    assert "nonexistent-ref" in str(exc.value)
    assert exc.value.code == "analysis_error"


async def test_default_head_minus_one_with_only_one_commit_raises(repo_root: Path):
    # only the initial commit exists; HEAD~1 should not resolve
    with pytest.raises(AnalysisError):
        await diff_against(repo_root)


async def test_non_git_directory_raises_analysis_error(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("hi\n")
    with pytest.raises(AnalysisError) as exc:
        await diff_against(plain, since="HEAD")
    assert "not a git repository" in str(exc.value)


async def test_results_sorted_by_path(repo_root: Path):
    repo = _repo(repo_root)
    _commit_all(repo, "baseline")
    (repo_root / "src" / "z.ts").write_text("1\n")
    (repo_root / "src" / "a.ts").write_text("1\n")
    (repo_root / "src" / "m.ts").write_text("1\n")
    _commit_all(repo, "abc")

    changes = await diff_against(repo_root)
    paths = [c.path.as_posix() for c in changes]
    assert paths == sorted(paths)
