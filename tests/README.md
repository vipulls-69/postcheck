# Postcheck — running the test suite

```bash
make test           # unit tests only (fast, no browser)
make test-int       # unit + integration tests (some require Chrome)
```

## Markers

- `requires_chrome` — needs a manually-launched Chrome instance reachable
  over the Chrome DevTools Protocol. Skipped by default.

## Environment-gated browser tests

| Env var                         | Gates                                                                                                                    |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `POSTCHECK_RUN_CHROME_TESTS=1`  | `tests/integration/test_cdp_attach.py` success path                                                                      |
| `POSTCHECK_RUN_BROWSER_TESTS=1` | `tests/integration/test_scenario_runner.py` (launches Chromium via Playwright + serves a static page over loopback HTTP) |

> **Known issue:** Playwright 1.59 + Python 3.14 on macOS arm64 SIGKILLs
> the headless shell on launch. Run the `POSTCHECK_RUN_BROWSER_TESTS`
> suite on Python 3.13 or a Linux runner until the upstream fix lands.

To register the marker quietly, add to `pytest.ini`:

```ini
[pytest]
markers =
    requires_chrome: needs a manually-launched Chrome (CDP)
```

## Running CDP-attach success-path tests locally

The CDP-attach failure test always runs (it expects no Chrome on a free
ephemeral port). The success-path test is gated behind two conditions:

1. `POSTCHECK_RUN_CHROME_TESTS=1` in the environment.
2. A Chrome process listening on `POSTCHECK_CHROME_PORT` (default `9222`).

### 1. Launch Chrome with a dedicated profile

A dedicated `--user-data-dir` is required because Chrome locks the default
profile.

**macOS:**

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.postcheck-chrome-profile"
```

**Linux:**

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.postcheck-chrome-profile"
```

**Windows (PowerShell):**

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:USERPROFILE\.postcheck-chrome-profile"
```

Leave the window open. You can log into your app once in this profile and
the cookies will persist for subsequent verification runs.

### 2. Run the integration tests

```bash
POSTCHECK_RUN_CHROME_TESTS=1 \
  .venv/bin/python -m pytest tests/integration/test_cdp_attach.py -v
```

The success-path test opens a _new_ browser context (not a new browser),
runs `page.goto("about:blank")` and a trivial JS evaluation, then closes
the context. Your existing tabs are never touched.

If the env var is unset, or nothing is listening on the configured port,
the success-path test is skipped with an explanatory `reason`.

## Saving an authenticated session for headless / CI

In headless or CI runs we cannot prompt for login. Capture a Playwright
`storage_state` JSON once, manually, then check the file in (encrypted /
secret-managed):

```python
import asyncio
from pathlib import Path
from postcheck.browser.cdp_attach import CDPAttachConfig, attach
from postcheck.browser.session_bridge import save_storage_state

async def main() -> None:
    session = await attach(CDPAttachConfig(port=9222))
    try:
        # Use session.context to log in interactively in a tab, then:
        await save_storage_state(session.context, Path(".postcheck/state.json"))
    finally:
        await session.close()

asyncio.run(main())
```

Pass that file via `CDPAttachConfig(storage_state_path=...)` (or
`VerifyOptions.storage_state_path` once wired through the orchestrator).
