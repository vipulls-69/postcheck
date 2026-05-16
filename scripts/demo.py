#!/usr/bin/env python3
"""End-to-end Postcheck demo runner.

Builds a fresh 2-commit git history for ``examples/demo_ecommerce/``
(clean baseline -> buggy "refactor" commit), boots the Vite dev server
with its inline fake backend, sanity-checks that the buggy code still
compiles, and then runs the verification engine in-process.

The output is intended to be read on camera: numbered steps, no Python
tracebacks unless something legitimately broke.

Usage:

    python scripts/demo.py [--keep] [--no-build] [--workspace DIR]

    --keep        Don't delete the temp workspace at the end.
    --no-build    Skip ``npm run build`` (still runs typecheck).
    --workspace   Use a specific directory instead of /tmp/postcheck-demo-XXXX.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SRC = REPO_ROOT / "examples" / "demo_ecommerce"
BUGS_DIR = DEMO_SRC / "bugs"

# Files copied from bugs/ onto the clean tree, keyed by source-tree path.
BUG_OVERLAYS: dict[str, str] = {
    "Cart.tsx": "src/routes/Cart.tsx",
    "Checkout.tsx": "src/routes/Checkout.tsx",
    "Profile.tsx": "src/routes/Profile.tsx",
    "cart-state.ts": "src/lib/cart-state.ts",
}

DEV_HOST = "127.0.0.1"
DEV_PORT = 5173
DEV_URL = f"http://{DEV_HOST}:{DEV_PORT}"
CDP_PORT = 9222


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

class _C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    RESET = "\033[0m"


def step(n: int, total: int, title: str) -> None:
    print()
    print(f"{_C.BOLD}{_C.BLUE}[{n}/{total}]{_C.RESET} {_C.BOLD}{title}{_C.RESET}")


def info(msg: str) -> None:
    print(f"  {_C.DIM}·{_C.RESET} {msg}")


def ok(msg: str) -> None:
    print(f"  {_C.GREEN}✓{_C.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {_C.YELLOW}!{_C.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {_C.RED}✗{_C.RESET} {msg}", file=sys.stderr)


def pause(prompt: str) -> None:
    print()
    try:
        input(f"  {_C.CYAN}↳ {prompt}{_C.RESET} ")
    except EOFError:
        # Non-interactive: just continue.
        print()


# ---------------------------------------------------------------------------
# Workspace setup
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, cwd: Path, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, surface a clean error on failure."""
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        fail(f"{' '.join(cmd)} (cwd={cwd}) failed")
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)
        raise SystemExit(proc.returncode)
    return proc


def build_workspace(target: Path) -> None:
    """Copy clean source -> two-commit history -> overlay bugs."""
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    # Copy clean source (skip bugs/, node_modules, .postcheck).
    ignore = shutil.ignore_patterns("bugs", "node_modules", ".postcheck", "dist", ".git")
    for entry in DEMO_SRC.iterdir():
        if entry.name in {"bugs", "node_modules", ".postcheck", "dist", ".git"}:
            continue
        if entry.is_dir():
            shutil.copytree(entry, target / entry.name, ignore=ignore)
        else:
            shutil.copy2(entry, target / entry.name)
    info(f"copied clean source -> {target}")

    # git init + clean baseline commit
    git_env = {
        "GIT_AUTHOR_NAME": "Postcheck Demo",
        "GIT_AUTHOR_EMAIL": "demo@postcheck.local",
        "GIT_COMMITTER_NAME": "Postcheck Demo",
        "GIT_COMMITTER_EMAIL": "demo@postcheck.local",
    }
    _run(["git", "init", "-q", "-b", "main"], cwd=target, env=git_env)
    _run(["git", "add", "."], cwd=target, env=git_env)
    _run(["git", "commit", "-q", "-m", "demo: clean baseline"], cwd=target, env=git_env)
    ok("commit 1/2: demo: clean baseline")

    # Overlay buggy variants.
    for bug_name, rel_target in BUG_OVERLAYS.items():
        src = BUGS_DIR / bug_name
        dst = target / rel_target
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        info(f"applied bug: {rel_target}")

    _run(["git", "add", "."], cwd=target, env=git_env)
    _run(
        ["git", "commit", "-q", "-m", "feat: refactor cart and checkout flow"],
        cwd=target,
        env=git_env,
    )
    ok("commit 2/2: feat: refactor cart and checkout flow")


# ---------------------------------------------------------------------------
# Anti-cheat: typecheck + build on the buggy commit
# ---------------------------------------------------------------------------


def npm_install_if_needed(workspace: Path) -> None:
    if (workspace / "node_modules").is_dir():
        info("node_modules already present, skipping install")
        return
    info("running npm install (first run only) — this can take a minute")
    _run(["npm", "install", "--no-audit", "--no-fund", "--silent"], cwd=workspace)
    ok("dependencies installed")


def anti_cheat(workspace: Path, *, skip_build: bool) -> None:
    info("$ npm run typecheck")
    _run(["npm", "run", "typecheck", "--silent"], cwd=workspace)
    ok("typecheck passes")
    if skip_build:
        warn("--no-build: skipping `npm run build`")
    else:
        info("$ npm run build")
        _run(["npm", "run", "build", "--silent"], cwd=workspace)
        ok("build passes")
    print()
    print(f"  {_C.BOLD}{_C.GREEN}✓ Code compiles cleanly — but is it actually working?{_C.RESET}")


# ---------------------------------------------------------------------------
# Dev server
# ---------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


@contextmanager
def dev_server(workspace: Path) -> Iterator[subprocess.Popen]:
    if _port_open(DEV_HOST, DEV_PORT):
        fail(f"port {DEV_PORT} already in use — stop the other process first")
        raise SystemExit(1)
    info(f"spawning `npm run dev` (port {DEV_PORT})")
    log = open(workspace / "vite.log", "w")
    proc = subprocess.Popen(
        ["npm", "run", "dev", "--silent"],
        cwd=workspace,
        stdout=log,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid if os.name == "posix" else None,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                fail("vite exited early; check vite.log")
                raise SystemExit(1)
            if _http_ok(DEV_URL):
                ok(f"vite up at {DEV_URL}")
                break
            time.sleep(0.25)
        else:
            fail(f"vite didn't become ready within 30s; check {workspace}/vite.log")
            raise SystemExit(1)
        yield proc
    finally:
        info("stopping vite")
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except ProcessLookupError:
            pass
        log.close()


# ---------------------------------------------------------------------------
# Chrome guidance
# ---------------------------------------------------------------------------


def chrome_guidance() -> None:
    print()
    print(f"  {_C.BOLD}Launch Chrome with CDP enabled in a separate terminal:{_C.RESET}")
    print()
    print(f"  {_C.CYAN}# macOS{_C.RESET}")
    print(
        '    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\\n'
        f"      --remote-debugging-port={CDP_PORT} \\\n"
        '      --user-data-dir="$HOME/.postcheck-chrome-profile"'
    )
    print()
    print(f"  {_C.CYAN}# Linux{_C.RESET}")
    print(
        f"    google-chrome --remote-debugging-port={CDP_PORT} \\\n"
        '      --user-data-dir="$HOME/.postcheck-chrome-profile"'
    )
    print()
    print(f"  {_C.CYAN}# Windows{_C.RESET}")
    print(
        '    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^\n'
        f"      --remote-debugging-port={CDP_PORT} ^\n"
        '      --user-data-dir="%USERPROFILE%\\.postcheck-chrome-profile"'
    )
    print()
    print(f"  {_C.DIM}A dedicated --user-data-dir is required (Chrome locks the default profile).{_C.RESET}")


def cdp_ready() -> bool:
    return _http_ok(f"http://localhost:{CDP_PORT}/json/version", timeout=1.0)


# ---------------------------------------------------------------------------
# Run the verification engine in-process
# ---------------------------------------------------------------------------


async def run_postcheck(workspace: Path) -> int:
    # Import lazily so --help / dry runs don't pay the cost.
    from postcheck.core.orchestrator import run_verification
    from postcheck.core.types import VerifyOptions
    from postcheck.reporting.reporter import to_markdown

    # Point the orchestrator at the demo dev server.
    os.environ["POSTCHECK_BASE_URL"] = DEV_URL
    os.environ["POSTCHECK_CHROME_DEBUG_PORT"] = str(CDP_PORT)

    opts = VerifyOptions(
        project_root=workspace,
        since="HEAD~1",
        cdp_endpoint=f"http://localhost:{CDP_PORT}",
    )

    info(f"project_root = {workspace}")
    info(f"since        = HEAD~1")
    info(f"base_url     = {DEV_URL}")

    result = await run_verification(opts)

    print()
    print(f"{_C.BOLD}─── REPORT ───────────────────────────────────────────────{_C.RESET}")
    print()
    print(
        to_markdown(
            result.bugs,
            {
                "run_id": result.run_id,
                "project": str(workspace),
                "since": "HEAD~1",
                "status": result.status,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            },
        )
    )
    print(f"{_C.BOLD}──────────────────────────────────────────────────────────{_C.RESET}")
    print()
    if result.report_paths:
        info("reports written to:")
        for kind, path in result.report_paths.items():
            print(f"      {kind}: {path}")
    if result.error:
        fail(f"verification error: {result.error}")
        return 2
    if not result.bugs:
        warn("no bugs reported — was Chrome attached and was the app reachable?")
        return 1
    ok(f"{len(result.bugs)} bug(s) reported")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keep", action="store_true", help="don't delete the temp workspace")
    p.add_argument("--no-build", action="store_true", help="skip `npm run build` (still typechecks)")
    p.add_argument("--workspace", type=Path, default=None, help="use this directory instead of /tmp/postcheck-demo-XXXX")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    total = 7

    print(f"{_C.BOLD}Postcheck demo{_C.RESET}  —  examples/demo_ecommerce")

    step(1, total, "Chrome with --remote-debugging-port")
    chrome_guidance()
    if cdp_ready():
        ok(f"CDP endpoint already up at http://localhost:{CDP_PORT}")
    else:
        warn(f"CDP endpoint not detected at http://localhost:{CDP_PORT} yet")
    pause("Press Enter once Chrome is running with CDP enabled.")
    if not cdp_ready():
        fail("still no CDP — aborting. Re-launch Chrome with the command above.")
        return 1
    ok("CDP endpoint reachable")

    step(2, total, "Build a fresh 2-commit demo workspace")
    workspace = args.workspace or Path(tempfile.mkdtemp(prefix="postcheck-demo-"))
    build_workspace(workspace)

    step(3, total, "Install npm dependencies")
    npm_install_if_needed(workspace)

    step(4, total, "Anti-cheat: does the buggy commit actually compile?")
    anti_cheat(workspace, skip_build=args.no_build)

    step(5, total, "Boot the demo app (Vite + inline fake backend)")
    exit_code = 0
    try:
        with dev_server(workspace):
            print()
            info(f"the app is live at {DEV_URL}")
            info("(optional) open it in your CDP-enabled Chrome to look around")
            pause("Press Enter to run Postcheck against this workspace.")

            step(6, total, "Run the verification engine")
            exit_code = asyncio.run(run_postcheck(workspace))
    except KeyboardInterrupt:
        print()
        warn("interrupted")
        exit_code = 130

    step(7, total, "Done")
    if args.keep:
        info(f"workspace kept at {workspace}")
    else:
        info(f"cleaning up {workspace}")
        shutil.rmtree(workspace, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
