"""End-to-end verification tests.

Gated on ``POSTCHECK_RUN_BROWSER_TESTS=1`` because they require a Chrome
launched with ``--remote-debugging-port=9222``. The test mutates a copy of
``examples/plain_html_site`` so the working tree is never disturbed, serves
it via ``python -m http.server``, runs the full :func:`run_verification`
flow, and asserts that an injected runtime error is reported.
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import socketserver
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from typing import Iterator

import pytest

from postcheck.core.orchestrator import run_verification
from postcheck.core.types import VerifyOptions

pytestmark = [
    pytest.mark.requires_chrome,
    pytest.mark.skipif(
        os.environ.get("POSTCHECK_RUN_BROWSER_TESTS") != "1",
        reason="set POSTCHECK_RUN_BROWSER_TESTS=1 to run end-to-end browser tests",
    ),
]


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(*args: str, cwd: Path) -> None:
    subprocess.check_call(["git", "-C", str(cwd), *args])


@pytest.fixture
def served_site(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    src = Path(__file__).parents[2] / "examples" / "plain_html_site"
    dst = tmp_path / "site"
    shutil.copytree(src, dst)

    _git("init", "-q", cwd=dst)
    _git("config", "user.email", "t@t", cwd=dst)
    _git("config", "user.name", "t", cwd=dst)
    _git("config", "commit.gpgsign", "false", cwd=dst)
    _git("add", "-A", cwd=dst)
    _git("commit", "-q", "-m", "init", cwd=dst)

    js = dst / "shared.js"
    js.write_text(
        js.read_text().replace(
            'document.getElementById("home-output").textContent = "Hello from Home!";',
            'throw new Error("postcheck e2e injected failure");',
        )
    )

    port = _free_port()

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_a: object, **_kw: object) -> None:
            pass

        def __init__(self, *a: object, **kw: object) -> None:
            super().__init__(*a, directory=str(dst), **kw)  # type: ignore[arg-type]

    server = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield dst, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_runtime_error_is_caught_end_to_end(
    served_site: tuple[Path, str],
) -> None:
    project_root, base_url = served_site
    os.environ["POSTCHECK_BASE_URL"] = base_url
    try:
        opts = VerifyOptions(project_root=project_root, since="HEAD")
        result = await run_verification(opts)
    finally:
        os.environ.pop("POSTCHECK_BASE_URL", None)

    assert result.status == "completed"
    assert result.affected_routes, "expected at least one affected route"
    runtime_bugs = [b for b in result.bugs if b.probe == "runtime"]
    assert runtime_bugs, f"expected a runtime bug, got {[b.title for b in result.bugs]}"
    assert any(
        "postcheck e2e injected failure" in (b.detail or "")
        or "postcheck e2e injected failure" in str(b.evidence)
        for b in runtime_bugs
    )

    md_path = Path(result.report_paths["markdown"])
    assert md_path.exists()
    assert "postcheck e2e injected failure" in md_path.read_text()
