"""Git-backed diff analyzer (v0).

Returns a list of :class:`FileChange` between a base ref (default ``HEAD~1``)
and the current working tree, filtering lockfiles, generated directories, and
any path matching the configured ``exclude_globs``.
"""
from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import Path, PurePosixPath
from typing import Iterable

from git import GitCommandError, InvalidGitRepositoryError, Repo
from git.diff import Diff

from ..core.config import DEFAULT_EXCLUDE_GLOBS, load_settings
from ..core.errors import AnalysisError
from ..core.types import ChangeKind, FileChange, Hunk

# Hard-coded extras on top of config.exclude_globs. CLAUDE.md requires these
# specific entries always be filtered.
_BUILTIN_EXCLUDES: tuple[str, ...] = (
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/uv.lock",
    "**/Pipfile.lock",
    "**/dist/**",
    "**/build/**",
    "**/.next/**",
    "**/.vite/**",
)

_HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_lines>\d+))?"
    r"\s+\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))?\s+@@"
)

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".py": "python",
    ".json": "json",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def diff_against(
    project_root: Path,
    since: str | None = None,
    *,
    exclude_globs: list[str] | None = None,
) -> list[FileChange]:
    """Diff the working tree against ``since`` (default ``HEAD~1``).

    Args:
        project_root: Path to the project root (must contain a git repo).
        since: Git ref to diff against. Defaults to ``HEAD~1``.
        exclude_globs: Override config-loaded exclude globs. Builtin lockfile
            and build-output excludes are always applied on top.

    Returns:
        A list of :class:`FileChange` ordered by path. Empty when no
        non-excluded files differ.

    Raises:
        AnalysisError: project is not a git repo, ``since`` is missing/invalid,
            or git invocation fails.
    """
    if exclude_globs is None:
        exclude_globs = list(load_settings(project_root).exclude_globs)
    return await asyncio.to_thread(_diff_sync, project_root, since, exclude_globs)


# ---------------------------------------------------------------------------
# Sync core (run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _diff_sync(
    project_root: Path,
    since: str | None,
    exclude_globs: list[str],
) -> list[FileChange]:
    try:
        repo = Repo(project_root, search_parent_directories=False)
    except InvalidGitRepositoryError as exc:
        raise AnalysisError(
            f"{project_root} is not a git repository",
            context={"project_root": str(project_root)},
        ) from exc

    base = since or "HEAD~1"
    try:
        repo.commit(base)
    except (GitCommandError, ValueError, Exception) as exc:  # noqa: BLE001
        # gitpython raises BadName / GitCommandError; both surface as ValueError
        # subclasses or its own types in older versions. Catch broadly so we
        # always emit an AnalysisError, never bubble a raw git error.
        raise AnalysisError(
            f"Could not resolve base ref {base!r}: {exc}",
            context={"project_root": str(project_root), "since": base},
        ) from exc

    try:
        # create_patch=True gives us unified-diff text per Diff entry.
        diff_index = repo.commit(base).diff(
            other=None,  # working tree
            create_patch=True,
            # R=True enables rename detection
            R=False,
        )
    except GitCommandError as exc:
        raise AnalysisError(
            f"git diff failed: {exc}",
            context={"project_root": str(project_root), "since": base},
        ) from exc

    excludes = list(exclude_globs) + list(_BUILTIN_EXCLUDES)
    seen: set[str] = set()
    results: list[FileChange] = []

    for diff in diff_index:
        change = _diff_to_file_change(diff)
        if change is None:
            continue
        path_str = change.path.as_posix()
        if _is_excluded(path_str, excludes):
            continue
        if change.old_path is not None and _is_excluded(change.old_path.as_posix(), excludes):
            continue
        if path_str in seen:
            continue
        seen.add(path_str)
        results.append(change)

    results.sort(key=lambda c: c.path.as_posix())
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff_to_file_change(diff: Diff) -> FileChange | None:
    kind = _classify(diff)
    if kind is None:
        return None

    a_path = diff.a_path
    b_path = diff.b_path
    # Path used downstream: the post-change path for added/modified/renamed,
    # the pre-change path for deleted.
    target = b_path if kind != "deleted" else a_path
    if target is None:
        return None
    target_path = Path(PurePosixPath(target).as_posix())

    old_path: Path | None = None
    if kind == "renamed" and a_path is not None and a_path != b_path:
        old_path = Path(PurePosixPath(a_path).as_posix())

    return FileChange(
        path=target_path,
        kind=kind,
        old_path=old_path,
        hunks=list(_parse_hunks(diff)),
        language=_language_for(target_path),
    )


def _classify(diff: Diff) -> ChangeKind | None:
    if diff.renamed_file:
        return "renamed"
    if diff.new_file:
        return "added"
    if diff.deleted_file:
        return "deleted"
    if diff.a_blob is None and diff.b_blob is not None:
        return "added"
    if diff.a_blob is not None and diff.b_blob is None:
        return "deleted"
    if diff.a_path != diff.b_path and diff.a_path and diff.b_path:
        return "renamed"
    if diff.a_blob is not None and diff.b_blob is not None:
        return "modified"
    return None


def _parse_hunks(diff: Diff) -> Iterable[Hunk]:
    raw = diff.diff
    if raw is None:
        return
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    if not text:
        return

    current_header: re.Match[str] | None = None
    current_body: list[str] = []

    def _flush() -> Hunk | None:
        if current_header is None:
            return None
        body = "\n".join(current_body)
        return Hunk(
            old_start=int(current_header["old_start"]),
            old_lines=int(current_header["old_lines"] or 1),
            new_start=int(current_header["new_start"]),
            new_lines=int(current_header["new_lines"] or 1),
            content=body,
        )

    for line in text.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match:
            flushed = _flush()
            if flushed is not None:
                yield flushed
            current_header = match
            current_body = [line]
        elif current_header is not None:
            current_body.append(line)

    flushed = _flush()
    if flushed is not None:
        yield flushed


def _language_for(path: Path) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def _is_excluded(path: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if _glob_match(path, pattern):
            return True
    return False


def _glob_match(path: str, pattern: str) -> bool:
    """Globstar-aware match.

    ``fnmatch`` does not treat ``**`` as "any number of directories"; emulate
    that by also matching the pattern with the leading ``**/`` stripped, plus
    against any path suffix.
    """
    if fnmatch.fnmatch(path, pattern):
        return True
    if "**/" in pattern:
        bare = pattern.replace("**/", "")
        if fnmatch.fnmatch(path, bare):
            return True
        # Allow the pattern to match nested occurrences too
        parts = path.split("/")
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), bare):
                return True
    return False


__all__ = ["diff_against"]
