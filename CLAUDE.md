# Postcheck — Project Instructions

This file is the source of truth for Claude Code working in this repo. Read it before making changes. If anything here conflicts with a one-off user instruction in a session, ask before deviating.

---

## What this project is

Postcheck is a **post-completion verification system** that runs after an LLM finishes a coding task and the code compiles. It does what the LLM does not: exercise the changed code in a real browser, catch runtime errors, network failures, storage issues, and UI regressions, and report them back as a structured bug list. If the user then asks for a fix, a separate agent attempts it.

The tool exists because LLM coding agents declare "done" when syntax passes. Syntax passing is not behavior working. Postcheck closes that gap.

**This is a product, not just a Claude Code extension.** It is being built in stages: a CLI-first v0 to prove the verification engine, then API + MCP wrappers post-v0, then a web dashboard and advanced analysis in v1. The architecture is shaped so each stage adds to the previous without rewriting.

---

## Architecture shape: core + CLI (v0), wrappers added later

v0 is **CLI-first**. The verification engine is a pure Python library; the CLI is the only wrapper. The core is still wrapper-agnostic so that when the API and MCP server are added post-v0, they slot in without core changes.

```
                ┌─────────────────────────┐
                │     Postcheck Core      │
                │   (pure Python lib)     │
                │                         │
                │  analysis · browser ·   │
                │  probes · reporting     │
                └────────────┬────────────┘
                             │
                ┌────────────▼────────────┐
                │          CLI            │
                │     (Python, typer)     │
                │                         │
                │  + local SQLite for     │
                │    run history          │
                └─────────────────────────┘

   Post-v0 additions: FastAPI (HTTP), MCP server
   v1 additions:      Dashboard, advanced analysis
```

State lives in `.postcheck/postcheck.db` (SQLite) inside each project. Schema matches what the future API will use, so migration to a server-side database is a configuration swap, not a rewrite.

The core never imports the CLI. The CLI imports the core. When wrappers are added later, they will import from core exactly the same way the CLI does — no special accommodation.

---

## Tech stack (non-negotiable for v0)

**Core engine:**
- Python 3.11+ with strict typing (`from __future__ import annotations`, all functions typed)
- `pydantic` v2 for all data models
- `playwright` for browser automation (async API; supports both CDP-attach and launch modes)
- `tree-sitter` + language grammars (`tree-sitter-typescript`, `tree-sitter-javascript`, `tree-sitter-html`, `tree-sitter-css`)
- `networkx` for dependency graph operations
- `GitPython` for diff analysis
- `anthropic` SDK for Claude calls (v1 planner, fixer, vision-judge — not used in v0)

**Persistence and CLI:**
- `sqlmodel` (Pydantic + SQLAlchemy) for ORM
- `aiosqlite` for async SQLite access
- `alembic` for migrations (yes, even with SQLite — schema discipline from day one)
- `typer` for the CLI
- `structlog` for structured logging

**Testing:**
- `pytest` + `pytest-asyncio`
- `pytest-playwright` for browser integration tests
- Coverage via `pytest-cov`

**Lint/format:**
- `ruff` for linting and formatting
- `mypy --strict` for type checking

**Post-v0 (do not install yet):**
- `fastapi` + `uvicorn` for the HTTP API
- `celery` + Redis for async job processing (when the API is multi-user)
- `mcp` (official Python SDK) for the MCP server
- Next.js for v1 dashboard (separate workspace)

Do not introduce new dependencies without a comment in the PR explaining why. Especially avoid: anything with native compilation steps beyond what's listed, anything that adds a build step beyond Python tooling, anything framework-specific in the core.

---

## Core architectural principles

These are load-bearing. Violating them costs us rewrites later.

1. **The core is pure.** No CLI imports, no DB-layer imports inside core modules. The core takes a config and returns results. Wrappers handle I/O, persistence, and protocol. If `postcheck/core/*.py` imports anything from `postcheck/cli/` or `postcheck/db/`, that's a bug.

2. **Probes don't know about frameworks.** A runtime exception is a runtime exception whether it came from vanilla JS or React. Framework knowledge lives only in adapters. If you find yourself adding `if framework == "react":` inside a probe, stop and add the capability to the adapter contract instead.

3. **Adapters declare what they can answer.** Required methods cover routing; optional methods cover dependency graphs, state graphs, template-to-selector mapping. The planner widens its scope when optional methods are missing. Never assume an adapter implements an optional method.

4. **Impact mapper output is the schema hedge for v1.** Every affected route carries `{ route, reason, confidence, changed_symbols, suspected_selectors }` from v0 onwards. In v0 every entry is `{ reason: 'direct', confidence: 'high' }`. This costs nothing now and means v1's cross-route work doesn't require refactoring consumers.

5. **Fail open, never silently narrow.** When static analysis can't determine impact confidently, broaden scope to the whole route and tell the user. Silent narrowing that misses bugs is the worst possible failure mode — it makes the tool *anti-helpful*.

6. **Browser layer supports both attach and launch.** For local dev with a visible browser, the user runs Chrome with `--remote-debugging-port=9222 --user-data-dir=<dedicated>` and we attach via CDP — every cookie and token already there. For Codespaces, CI, and headless servers, Playwright launches its own Chromium inside the same environment as the dev server. The mode is `config.launch_mode: "attach" | "launch"`. Callers don't care which — `browser.get_browser(config)` dispatches.

7. **Bugs have provenance.** Every reported bug carries the probe that detected it, the interaction that triggered it, the file/line of the diff that likely caused it, and a confidence tag (`deterministic` | `heuristic` | `llm_judged`). Without provenance, users can't decide what to trust and will ignore the tool inside a week.

8. **Multi-tenancy is foundational, not retrofitted.** Every persisted record carries an `org_id` from v0, even though v0 ships single-tenant. Adding tenancy later requires backfilling and is painful. We pay the small cost upfront. This applies to CLI-local SQLite the same way it would apply to a server-side database.

9. **Schema is the contract.** Pydantic models in `core/types.py` and SQLModel tables in `db/models.py` are the contract between layers and across versions. Breaking changes go through Alembic migrations and explicit version bumps, never silent shape changes.

10. **Async by default in core.** All I/O-bound functions in `core/` are `async`. Playwright's async API is the primary one. SQLModel sessions are async via aiosqlite. CLI commands call `asyncio.run()` at the boundary. This avoids the sync/async impedance mismatch later when the API is added.

11. **CLI is the only v0 wrapper, but core stays wrapper-agnostic.** When API and MCP are added post-v0, they will import from core the same way CLI does. The "core never imports wrappers" rule has no exceptions. Don't bake CLI assumptions into core just because there's only one wrapper today.

---

## v0 vs post-v0 vs v1 — explicit scope

The single most important section of this file. **Do not implement post-v0 or v1 features in v0 PRs.** If a v0 task seems to need a later feature, surface it and we'll discuss.

### v0 — CLI-first working product

**Goal:** A real, usable command-line tool that runs verifications, persists history locally, and the author would miss if it were gone. No server, no API, no MCP yet. Verification engine has narrow but real capability.

**v0 IS:**

*Verification engine:*
- Diff analyzer (`GitPython`, returns typed `FileChange[]`)
- AST symbol diff via tree-sitter, function/class/export granularity
- Two route adapters:
  - `plain_html` — file-system routing, `<script>`/`<link>` tag dependency graph
  - `react_vite` — React Router v6/v7 config parsing + file-system routing, ES module dependency graph, JSX → selector mapping via tree-sitter
- Impact mapper: changed file → its own route only (no transitive analysis), but output schema supports v1's richer reasons
- Browser layer with both `attach` (CDP to user Chrome) and `launch` (Playwright spawns its own Chromium) modes
- All four probes running sequentially: runtime, network, storage, UI (DOM assertions only — no vision-judge)
- Network probe with three-layer filtering: resource type, URL pattern, origin (defaults exclude HMR, common telemetry)
- Scenario runner that navigates, waits, triggers interactions, collects probe events
- Bug aggregator (flat list, no dedup), Markdown + JSON reporter

*Persistence:*
- Local SQLite at `.postcheck/postcheck.db` via SQLModel + aiosqlite
- Alembic migrations versioned in the repo
- Tables: Organization (single default), Project, Run, Bug
- All tables carry `org_id` from v0
- Foreign keys enforced (PRAGMA foreign_keys=ON on SQLite)

*CLI:*
- `postcheck init [path]` — sets up `.postcheck/`, runs migrations, creates default org + project
- `postcheck verify [--since <ref>] [--output <path>] [--json] [--project <path>]` — runs orchestrator, persists run, prints report. Exit codes: 0 clean, 1 bugs found, 2 verifier failed, 3 config error
- `postcheck runs list [--project <path>] [--limit N] [--all]`
- `postcheck runs show <id-or-prefix> [--json] [--output <path>]`
- `postcheck projects list/add/remove`
- `postcheck doctor` — sanity checks for setup
- `postcheck --version`

*Operational:*
- Structured logging via `structlog` (JSON to `.postcheck/postcheck.log`, pretty to stderr when verbose, silent by default)
- Secret redaction on all log paths (REDACTION_PATTERNS for Bearer tokens, JWTs, API keys)
- Config layering (lowest → highest precedence): built-in defaults → global config file → project config file → `POSTCHECK_*` env vars → CLI flags
- Global config lives at `~/.config/postcheck/config.json` (XDG_CONFIG_HOME aware) or `%APPDATA%\postcheck\config.json` on Windows; project config at `.postcheck/config.json`
- `postcheck config {get,set,unset,edit}` for inspecting and modifying configuration; each field declares which layers it accepts (global-only, project-only, either) and the loader warns + skips fields persisted at the wrong layer (forward compatibility, never crash)
- Works on macOS, Linux (including Codespaces), and Windows

**v0 IS NOT:**

- FastAPI / HTTP API (post-v0)
- MCP server (post-v0 — added after CLI is dogfooded)
- Celery / Redis / async job processing (post-v0)
- Hook scripts for Claude Code or git (post-v0)
- Web dashboard (v1)
- Multi-user / multi-org features (schema ready, UI absent — single default org)
- Cross-route impact analysis (no reverse-graph traversal)
- Declared relations file
- Subagent fan-out
- Vision-judge UI probe
- Fixer agent
- Diff coverage proof
- Differential before/after testing
- Adversarial scenario generation
- Existing-test reuse (Playwright/Cypress detection)
- Perf or a11y probes
- Screenshot PII redaction (because no screenshots ship to LLMs in v0)
- Confidence-based suppression / `.postcheckignore`
- Any framework adapter beyond plain HTML and React/Vite

v0 scenario runner triggers only click-style interactions automatically (clicks on buttons, links, and elements with click handlers). Form submission via "fill required inputs and submit" may be added before v0 ships if dogfooding reveals it's needed. Hover, drag-and-drop, keyboard navigation, complex input typing patterns, and touch/pointer gestures are explicitly deferred to v1, where they're paired with the supporting analysis (vision-judge for hover, cross-route impact for drag targets, a11y probe for keyboard, adversarial test generation for input fuzzing).

**v0 "done" criteria:**

1. `postcheck init` in a fresh React/Vite project sets up the DB and config in under 10 seconds
2. `postcheck verify` on a deliberately broken commit catches the bug, persists the run, prints a readable report
3. `postcheck runs list` shows the run; `postcheck runs show <id>` retrieves the full report
4. Same flow works on `examples/demo_ecommerce`
5. End-to-end run on a real project takes under 30 seconds for a small diff
6. Cross-platform: works on macOS, Linux (Codespace), Windows
7. **The author has used it daily on at least one real personal project for two weeks and would miss it if it were gone**

That last criterion matters more than the others. It's the actual quality bar.

### Post-v0 — API and MCP wrappers

**Goal:** Wrap the proven core with HTTP and MCP surfaces so other tools (Claude Code, Cursor, CI systems) can drive it. The CLI gains a `--remote` flag to hit an API instead of running in-process.

**Post-v0 adds:**

- FastAPI app with versioned `/v1/` routes: `POST /v1/verify`, `GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/bugs`, `GET /healthz`
- API key auth (Bearer tokens, hashed in DB)
- Celery + Redis for async job processing once the API is multi-user (single-instance API can run sync in-process initially)
- PostgreSQL as the backing DB when running as a server (same schema as SQLite v0 — Alembic handles the dialect difference)
- Python MCP server exposing `verify_changes` tool, calling the FastAPI API
- CLI gains `--remote <url>` flag; defaults to local execution when not specified
- Claude Code `Stop` hook and git pre-push hook scripts
- Docker Compose for running the API stack locally

The verification engine itself does not change in post-v0. The orchestrator, analysis, browser, probes, and reporting modules are all v0-stable.

### v1 — Make it smart and shippable as SaaS

**Goal:** Differentiated analysis capability plus the product surface that makes it sellable as SaaS.

**v1 verification engine adds:**

- Cross-route impact analysis
  - Reverse dependency graph (`networkx`-backed): for every exported symbol, "who imports me," walked transitively
  - Generic state read/write tracker (works across Zustand, Jotai, Context, Redux without library-specific code first)
  - API contract change detection
  - Style/global side-effect propagation
  - Routing config change propagation (guards, middleware, layouts)
- Declared relations
  - `.postcheck/relations.yml` loader with Pydantic schema validation
  - Stale-relation detection
  - Merge declared relations into impact set with confidence `explicit`
- Claude-as-fuzzy-matcher layer over static + declared impact; suggestions surface as low-confidence additions
- Subagent fan-out when impact crosses N routes (configurable, default 3), with a hard token budget cap
- Diff coverage proof via CDP `Profiler.startPreciseCoverage` — report % of changed lines that actually executed
- Differential before/after testing — exercise scenarios against pre-edit and post-edit code in worktrees, diff outputs
- Fixer agent with `max_rounds`, safety rail (abort if fix produces more bugs than original), and secret prompt subsystem for interactive credential collection
- Confidence scoring on every bug + `.postcheckignore` suppression
- Bug deduplication via causal grouping
- Adapter contract stabilization — formally locked in v1, both v0 adapters refactored if needed
- Third adapter: one of Next.js full, Vue, Svelte, or Angular, chosen by user demand
- Several v1 features unlock new interaction modes for the scenario runner: vision-judge enables reliable hover testing (tooltip visibility judged visually); cross-route impact analysis lets drag-and-drop scenarios know valid source/target pairs; the a11y probe brings keyboard navigation under axe-core; adversarial scenario generation handles controlled-input fuzzing with multiple value patterns. These features should ship together as the "expanded interaction model" v1 milestone, not piecemeal.

**v1 product surface adds:**

- Web dashboard (Next.js + shadcn/ui)
  - Verification history, run details, bug detail pages
  - Trends: bugs over time, hotspots by file, regression patterns
  - Project management
  - Team member invitations, role-based access (admin, member, viewer)
- Webhooks: post-verification notifications to Slack, Discord, Linear, GitHub Issues
- Billing and quotas (Stripe integration, usage-based pricing — runs per month)
- OAuth login (Google, GitHub) on top of API key auth
- Project auto-detection when API is called from CI — match repo URL to a project record

**v1 may add (decide based on v0 and post-v0 learnings):**

- Vision-judge UI probe (with PII redaction preprocessing)
- Perf probe (long tasks, layout thrash, memory growth, bundle delta)
- a11y probe (axe-core injected via Playwright)
- Adversarial scenario generation (sad-path probing)
- Existing-test reuse (Playwright/Cypress test detection and invocation)
- GitHub App for PR comments

---

## Repo structure

```
postcheck/
├── pyproject.toml
├── alembic.ini
├── Makefile
├── CLAUDE.md                       # this file
├── README.md
│
├── postcheck/                      # the Python package
│   ├── __init__.py
│   │
│   ├── core/                       # v0 — pure verification logic
│   │   ├── __init__.py
│   │   ├── orchestrator.py
│   │   ├── planner.py              # v0: trivial fixed plan; v1: Claude-backed
│   │   ├── config.py               # pydantic Settings
│   │   ├── subagent_pool.py        # v1 only — stub in v0
│   │   ├── errors.py               # PostcheckError hierarchy
│   │   └── types.py                # shared pydantic models
│   │
│   ├── analysis/                   # v0 (impact mapper direct-only in v0)
│   │   ├── __init__.py
│   │   ├── diff_analyzer.py
│   │   ├── ast_symbol_diff.py      # tree-sitter
│   │   ├── route_resolver/
│   │   │   ├── __init__.py
│   │   │   ├── contract.py         # Protocol — STABLE from v0
│   │   │   ├── registry.py         # adapter detection + dispatch
│   │   │   └── adapters/
│   │   │       ├── __init__.py
│   │   │       ├── plain_html.py   # v0
│   │   │       ├── react_vite.py   # v0
│   │   │       ├── next.py         # v1+
│   │   │       ├── vue.py          # v1+
│   │   │       ├── svelte.py       # v1+
│   │   │       └── angular.py      # v1+
│   │   ├── impact_mapper.py
│   │   ├── reverse_dep_graph.py    # v1 only — stub in v0
│   │   └── relation_loader.py      # v1 only — stub in v0
│   │
│   ├── browser/                    # v0 — supports attach and launch modes
│   │   ├── __init__.py             # get_browser() dispatch
│   │   ├── cdp_attach.py           # attach() and launch_browser()
│   │   ├── session_bridge.py       # storage_state import helpers
│   │   ├── target_locator.py
│   │   └── scenario_runner.py
│   │
│   ├── probes/                     # v0
│   │   ├── __init__.py
│   │   ├── shared.py               # ProbeHandler protocol, correlation helpers
│   │   ├── runtime_probe.py
│   │   ├── network_probe.py        # v0: 3-layer filter; v1: layers 4-5
│   │   ├── storage_probe.py
│   │   ├── ui_probe/
│   │   │   ├── __init__.py
│   │   │   ├── dom_assertions.py   # v0
│   │   │   └── visual_judge.py     # v1 only — stub
│   │   ├── coverage_probe.py       # v1 only — stub
│   │   ├── perf_probe.py           # v1 only — stub
│   │   └── a11y_probe.py           # v1 only — stub
│   │
│   ├── reporting/                  # v0
│   │   ├── __init__.py
│   │   ├── bug_aggregator.py       # v0: flat; v1: causal grouping
│   │   ├── severity_ranker.py      # v1 only — stub
│   │   └── reporter.py             # markdown + JSON
│   │
│   ├── fixer/                      # v1 only — empty in v0
│   │   ├── __init__.py
│   │   ├── fixer_agent.py
│   │   └── secret_prompt.py
│   │
│   ├── db/                         # v0 — local SQLite persistence
│   │   ├── __init__.py
│   │   ├── models.py               # SQLModel tables
│   │   ├── session.py              # async session factory, init_db helper
│   │   ├── repository.py           # query/persistence helpers
│   │   └── migrations/             # Alembic
│   │       ├── env.py
│   │       └── versions/
│   │
│   └── cli/                        # v0 — typer app
│       ├── __init__.py
│       ├── main.py                 # typer app, global options, error handler
│       ├── utils.py                # shared helpers (run ID resolver, etc.)
│       └── commands/
│           ├── __init__.py
│           ├── init.py
│           ├── verify.py
│           ├── runs.py
│           ├── projects.py
│           └── doctor.py
│
├── examples/                       # v0 — test fixtures
│   ├── plain_html_site/
│   ├── react_vite_app/
│   └── demo_ecommerce/             # the multi-bug demo project
│
├── scripts/                        # development helpers
│   └── run_orchestrator.py         # direct orchestrator runner (pre-CLI)
│
└── tests/
    ├── unit/                       # fast, no browser
    │   ├── test_types.py
    │   ├── test_errors.py
    │   ├── test_config.py
    │   ├── test_diff_analyzer.py
    │   ├── test_ast_symbol_diff.py
    │   ├── test_route_resolver/
    │   ├── test_impact_mapper.py
    │   ├── test_reporting.py
    │   ├── test_db/
    │   └── test_cli/
    └── integration/                # browser involved
        ├── test_cdp_attach.py
        ├── test_launch_browser.py
        ├── test_scenario_runner.py
        ├── test_probes/
        └── test_v0_acceptance.py   # the gate test
```

Post-v0 will add `api/`, `jobs/`, `mcp_server/`, and `hooks/` directories at the top of `postcheck/`. Do not create them in v0.

---

## Module specifications

What each module does, what it must expose, and which version it ships in. Anything tagged **v0** is in scope now. Anything tagged **v1** is a stub or empty module in v0. Anything tagged **post-v0** does not exist yet.

### `core/orchestrator` (v0)

Entrypoint for a single verification run from the core's perspective. Stateless from the caller's view — callers pass in config + diff context, get back results. Persistence is the caller's responsibility (the CLI's `db/repository.py` handles it).

Exposes: `async def run_verification(opts: VerifyOptions) -> VerifyResult`

### `core/planner` (v0 trivial, v1 real)

v0: returns a fixed plan — run all four probes sequentially against each affected route. No Claude call.

v1: Claude-backed agent (anthropic SDK) that decides parallel vs sequential, whether to spawn subagents, and how many. Hard token budget cap.

### `core/config` (v0)

`pydantic_settings.BaseSettings`-based config. Loaded from `.postcheck/config.json`, environment variables (`POSTCHECK_*` prefix), and defaults. Fields cover adapter selection, browser settings (including `launch_mode`, `launch_headless`, `launch_args`), network filter defaults, timeouts. `REDACTION_PATTERNS` module-level constant for secret redaction in logs.

### `core/errors` (v0)

`PostcheckError` base class with `.to_dict()` for structured logging. Typed subclasses: `CDPAttachError` (includes relaunch commands for all platforms), `AdapterDetectionError`, `ScenarioExecutionError`, `ConfigError`, `AnalysisError`. Never raise bare exceptions or strings from core code.

### `core/types` (v0)

Shared pydantic models: `FileChange`, `SymbolChange`, `Symbol`, `Route`, `Selector`, `AffectedRoute`, `Interaction`, `ProbeEvent`, `Bug`, `VerifyOptions`, `VerifyResult`, `SymbolGraph`, `StateGraph`, `LocatedTarget`, `LocationFailure`. The contract between all modules. **Changes here ripple everywhere — discuss before modifying.**

### `analysis/diff_analyzer` (v0)

`GitPython`-based. Async function `diff_against(project_root, since)` returns `list[FileChange]`. Filters lockfiles, generated code, and `exclude_globs` from config.

### `analysis/ast_symbol_diff` (v0)

`tree-sitter` for TS/JS/HTML/CSS. Returns `list[SymbolChange]`. CSS at file granularity in v0. Unknown file types skipped silently.

### `analysis/route_resolver/contract.py` (v0 — STABLE)

The adapter Protocol. Treat as stable from v0 onwards. File header comment marks it as such.

```python
class RouteAdapter(Protocol):
    name: str

    async def detect(self, project_root: Path) -> bool: ...
    async def list_routes(self, project_root: Path) -> list[Route]: ...
    async def files_for_route(self, route: Route) -> list[Path]: ...

    # Optional — adapters return None if unsupported
    async def dependency_graph(self) -> SymbolGraph | None: ...
    async def state_graph(self) -> StateGraph | None: ...
    async def template_selector_map(self, symbol: Symbol) -> list[Selector] | None: ...
```

### `analysis/route_resolver/adapters/plain_html` (v0)

File-system routing: every `.html` is a route. `<script>` and `<link>` tags for dependency graph. `template_selector_map` returns None.

### `analysis/route_resolver/adapters/react_vite` (v0)

React Router config detection (`createBrowserRouter`, `<Routes>`, route arrays) via tree-sitter; falls back to file-system routing. ES module import graph. JSX parsing maps `onClick={handler}` to selector candidates (data-testid, role + name, tag + text).

### `analysis/impact_mapper` (v0)

Direct-mapping only in v0. Output schema supports v1's full reason/confidence union. Do not change `AffectedRoute` type in v1 without a migration plan.

### `analysis/reverse_dep_graph` (v1 only)

Stub in v0.

### `analysis/relation_loader` (v1 only)

Stub in v0.

### `browser/__init__.py` (v0)

Exports `get_browser(config) -> (browser, context)` which dispatches to `cdp_attach.attach()` or `cdp_attach.launch_browser()` based on `config.launch_mode`. Callers always go through `get_browser`.

### `browser/cdp_attach.py` (v0)

Two async functions:
- `attach(config) -> (browser, context)` — `playwright.chromium.connect_over_cdp(...)`. On failure, raises `CDPAttachError` with platform-specific relaunch commands.
- `launch_browser(config) -> (browser, context)` — `playwright.chromium.launch(...)` with config's `launch_headless` and `launch_args`. For Codespaces, CI, headless servers.

Both return the same (browser, context) tuple shape. Both create a fresh context — never touch existing tabs in the attach case.

### `browser/session_bridge.py` (v0 partial)

`load_storage_state(path)` and `save_storage_state(context, path)`. For CI auth reuse via Playwright's storage_state mechanism. v1: interactive auth-once flow.

### `browser/target_locator.py` (v0)

Resolves `AffectedRoute.suspected_selectors` to live Playwright locators. Tries selectors in order; first match wins. Unmatched targets become `LocationFailure` objects, which the scenario runner converts to bugs ("changed handler X but couldn't find an element bound to it"). v1: vision-LLM fallback.

### `browser/scenario_runner.py` (v0)

Per route: navigate, wait (`networkidle` or `domcontentloaded` per config), attach probe handlers, fire default interactions for each located target, collect events. Fails soft — missing locator → bug, not crash.

### `probes/shared.py` (v0)

`ProbeHandler` protocol (`name`, `async attach(page)`, `collect_events() -> list[ProbeEvent]`, `async detach()`). Shared interaction context (the running list of interactions, queryable by probes to correlate events).

### `probes/runtime_probe` (v0)

`page.on("pageerror")`, console errors/warnings, unhandled rejections, crashes. Correlates with most recent interaction.

### `probes/network_probe` (v0)

Three-layer filter from `config.network`:
1. **Resource type** — default whitelist: `["xhr", "fetch", "websocket", "eventsource", "document"]`
2. **URL patterns** — `ignore_patterns` and `focus_patterns` (defaults exclude HMR, common telemetry)
3. **Origin** — cross-origin gets `confidence: medium`, same-origin in focus gets `confidence: high`

Flags 4xx/5xx, CORS failures, timeouts. `document` failures emitted as `navigation_failure`, not `network_error`. v1 adds protocol-level error shapes and diff-relevance scoring.

### `probes/storage_probe` (v0)

Injected via `add_init_script`. Wraps localStorage, sessionStorage, IndexedDB. Catches quota errors and serialization failures. Records writes/reads for downstream correlation.

### `probes/ui_probe/dom_assertions` (v0)

After each interaction: did the targeted element change? Visible? Not covered? Expected text content (when adapter predicted it)?

### `probes/ui_probe/visual_judge` (v1 only)

Stub.

### `reporting/bug_aggregator` (v0)

Flat list, grouped by probe, ordered by route then detection time. No dedup. v1: causal grouping.

### `reporting/reporter` (v0)

`to_markdown()` and `to_json()`. `write_report()` writes both to `.postcheck/runs/<run-id>/`. Each bug: probe, route, interaction, error/symptom, suspected diff file:line, confidence tag.

### `fixer/*` (v1 only)

Empty in v0.

### `db/models.py` (v0)

SQLModel tables, all with `id` UUID, `created_at`, `updated_at` UTC. Domain tables include `org_id`.

- `Organization` — id, name, slug, created_at, updated_at
- `Project` — id, org_id, name, local_path, default_adapter (nullable), config_overrides (JSON), created_at, updated_at
- `Run` — id, org_id, project_id, status (Literal: running, succeeded, failed, errored), since_ref, started_at, finished_at, total_bugs, report_json (JSON: full VerifyResult)
- `Bug` — id, org_id, run_id, probe, route, interaction_summary, title, detail, suspected_file (nullable), suspected_line (nullable int), confidence (Literal: deterministic, heuristic, llm_judged), raw_event (JSON)
  - The earlier single `error_message` column was retired in favour of the `title` (one-line headline) + `detail` (multi-line body) split. The single-field shape conflated headline and body, producing unreadable rows in both CLI table output and the persisted JSON. The new split matches standard error-reporting patterns and what the core `Bug` model has always exposed. Migration `0002_bug_split_message` performs the rename and backfills `title` from any pre-existing `error_message` values.

Indexes: Run on `(project_id, started_at DESC)`, Bug on `run_id`. Foreign keys enforced via SQLite PRAGMA.

### `db/session.py` (v0)

Async engine via aiosqlite. `async_sessionmaker`, `get_session()` async context manager. `init_db(project_root)` creates `.postcheck/`, the DB file, and runs `alembic upgrade head` to current schema. Idempotent.

### `db/repository.py` (v0)

Persistence helpers. All async, all take `session` and `org_id`. Functions: `get_or_create_default_org`, `list_projects`, `get_project_by_path`, `create_project`, `delete_project`, `create_run`, `finalize_run`, `list_runs`, `get_run`, `get_bugs_for_run`, `resolve_run_id_prefix`. Every query filters by `org_id` — no exceptions.

### `db/migrations/` (v0)

Alembic-managed. First revision `0001_initial_schema`. Migrations are append-only — never edit a shipped migration.

### `cli/main.py` (v0)

typer app. Global options: `--config`, `--verbose`, `--version`. Top-level exception handler: `PostcheckError` → friendly print + appropriate exit code; unexpected → "rerun with --verbose" + log file. Subcommand groups: `init`, `verify`, `runs`, `projects`, `doctor`. Exposed as `postcheck` script entry point in pyproject.toml.

### `cli/commands/init.py` (v0)

`postcheck init [path] [--yes]`. Creates `.postcheck/`, config.json with defaults, initializes DB, seeds default org, detects adapter, creates Project row.

### `cli/commands/verify.py` (v0)

`postcheck verify [--since <ref>] [--output <path>] [--json] [--project <path>]`. Finds project, loads config, creates Run row, runs orchestrator in-process, finalizes Run, prints report. Exit codes: 0 no bugs, 1 bugs found, 2 verifier failed, 3 config error.

### `cli/commands/runs.py` (v0)

`postcheck runs list [--project <path>] [--limit N] [--all]` — table output, columns: short id, project, started, duration, status, bugs.

`postcheck runs show <id-or-prefix> [--json] [--output <path>]` — renders the persisted report. Resolves run ID by prefix (first 8 chars, ambiguity errors clearly).

### `cli/commands/projects.py` (v0)

`postcheck projects list`, `postcheck projects add [path] [--name N] [--adapter A]`, `postcheck projects remove <id-or-path> [--yes]` (cascade-deletes runs).

### `cli/commands/doctor.py` (v0)

Sanity checks: Python version, Playwright browsers installed, git available, cwd has `.postcheck/`, DB readable, dev server reachable at `config.baseUrl`. Prints checklist with ✓/✗. Exit 0 if all pass, 1 if any fail.

### `cli/commands/config.py` (v0)

Layered-config management. Subcommands: `get [key] [--show] [--global] [--project]`, `set <key> <value> [--global]`, `unset <key> [--global]`, `edit [--global]`. Defaults to writing the project config; `--global` targets the user-global file. Validates that the target layer accepts the field (e.g. `set launch_mode --global` is allowed; `set launch_mode` at project layer is rejected with a hint). Accepts dotted keys for nested fields (`network.treat_cross_origin_as`); list/dict values must be JSON-encoded. Re-validates the full merged config before persisting; never writes if the result would be invalid. Unknown keys get close-match suggestions via `difflib.get_close_matches`.

### `cli/utils.py` (v0)

Shared CLI helpers: project root discovery (walk up from cwd looking for `.postcheck/`), run ID prefix resolution, table rendering, color-respecting print.

---

## Conventions

### Python code style

- `from __future__ import annotations` at top of every file
- All functions and methods fully typed; `mypy --strict` passes
- All public APIs documented with docstrings (param/return/raises)
- Errors are typed `PostcheckError` subclasses, never bare `Exception` or strings
- Async by default for I/O; sync only for pure CPU work
- No `print()` in core or db layers — use `structlog.get_logger()`. CLI may print directly (it's the user-facing surface)
- Pydantic v2 for all data models; no raw dicts crossing module boundaries
- No `Any` without `# reason:` comment
- One module per file under ~400 lines; split larger modules

### File and module boundaries

- `core/`, `analysis/`, `browser/`, `probes/`, `reporting/`, `fixer/` are **pure core** — they import only from each other and from external libs
- `db/`, `cli/` are **wrappers** — they import core but core never imports them
- Probes import only from `probes/shared.py` and Playwright — never analysis or core types beyond what `probes/shared.py` re-exports
- The orchestrator wires things; it's the only core module allowed to import across all core subpackages

### Database conventions

- All tables: `id` UUID, `created_at`, `updated_at` (UTC, no naive datetimes)
- Domain tables have `org_id`
- Migrations are append-only — never edit a shipped migration
- All queries scoped by `org_id` at the repository layer; never write a query that bypasses tenancy
- SQLite-specific: `PRAGMA foreign_keys=ON` set on every connection via SQLAlchemy event listener

### Configuration model

Configuration is loaded by `postcheck.core.config.load_config()` from five layers, lowest → highest precedence:

```
DEFAULT  →  GLOBAL  →  PROJECT  →  ENV (POSTCHECK_*)  →  FLAG (CLI)
```

- **DEFAULT** — hard-coded in `Settings` field defaults.
- **GLOBAL** — `~/.config/postcheck/config.json` (XDG_CONFIG_HOME aware) or `%APPDATA%\postcheck\config.json` on Windows. Resolved via `get_global_config_path()`.
- **PROJECT** — `<project_root>/.postcheck/config.json`.
- **ENV** — `POSTCHECK_*` environment variables; nested via `__` (e.g. `POSTCHECK_NETWORK__TREAT_CROSS_ORIGIN_AS`). Secrets (`DATABASE_URL`, `REDIS_URL`, `ANTHROPIC_API_KEY`) are env-only.
- **FLAG** — `cli_overrides` dict supplied by the CLI at runtime; never persisted.

Every `Settings` field declares its allowed layers via `Field(json_schema_extra={"layers": [...]})`. If a config file persists a value for a layer it isn't allowed at (e.g. `base_url` in global, `launch_mode` in project), the loader emits a `structlog` warning and skips the field. Files persist across versions — never crash on unknown or layer-misplaced keys.

Field policy (v0):

- **Global-only**: `launch_mode`, `chrome_debug_port`, `chrome_profile_dir`, `default_adapter_override`, `color_mode`, `verbose_default`.
- **Project-only**: `base_url`, `adapter`, `exclude_globs`.
- **Either** (project overrides global): `launch_headless`, `launch_args`, `timeout_ms`, `network.*`.
- **Env-only** (secrets): `database_url`, `redis_url`, `anthropic_api_key`.
- **Flag-only** (never persisted): `--since`, `--output`, `--json`, `--project`, `--verbose`.

`load_config_with_provenance()` additionally returns a `{field_path: ConfigLayer}` map for `postcheck config get --show`. When extending the schema, add `json_schema_extra={"layers": [...]}` to every new `Field`.

### CLI conventions

- Exit codes documented in `--help`: 0 success no bugs, 1 success with bugs, 2 verifier failed, 3 config/usage error
- Friendly error messages — Python tracebacks only with `--verbose`
- Respect `NO_COLOR` env var
- All commands resolve project root by walking up from cwd unless `--project` is given
- Confirmation prompts on destructive operations (delete, remove); `--yes` skips
- `--json` flag wherever it makes sense, for scripting

### Testing

- Unit tests in `tests/unit/` — no network, no browser, in-memory SQLite for DB tests
- Integration tests in `tests/integration/` — real Playwright (launch mode for portability across Codespaces and local), real SQLite via temp dir
- Test names describe the bug they prevent: `def test_impact_mapper_handles_null_dep_graph_from_adapter()`
- Browser tests use `pytest-playwright`; no mocking Playwright

### Commits

- Conventional commits (`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`)
- Scope is the package (`feat(cli): add doctor command`)
- v0/post-v0/v1 tag in the body when relevant
- Each commit should pass tests on its own

### Don't

- Don't add a framework adapter just because it's easy — each adapter is ongoing maintenance
- Don't expand `core/types` or `db/models` without updating this file and discussing
- Don't make probes framework-aware
- Don't make core import from wrappers (`db/` or `cli/`)
- Don't catch and swallow Playwright errors — they're signals
- Don't read or write outside the project root and `.postcheck/` directory at runtime
- Don't make outbound network calls except to Anthropic API (v1) and the user's local dev server
- Don't write to disk except `.postcheck/` inside the project
- Don't skip multi-tenancy enforcement "because we're single-tenant for now" — every query scoped by `org_id`
- Don't build post-v0 features in v0 — surface the question, decide explicitly

---

## Auth and security

### Local dev (CLI)

Two modes:

**Attach mode** — for visible-browser local development. User runs Chrome with debug port:

```
chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.postcheck-chrome-profile
```

Dedicated profile dir is required (Chrome locks the default profile while running). First-time setup: user logs into their app once; session persists across runs. Postcheck attaches via CDP and inherits everything.

**Launch mode** — for Codespaces, CI, and headless environments. Playwright spawns its own Chromium inside the same environment as the dev server. No user-controlled browser. For auth-protected apps in this mode, use `session_bridge` to load a Playwright `storage_state` JSON.

Mode is selected via `config.launch_mode`. Default: `launch` when no display detected, `attach` otherwise — but explicit is better than implicit; users should set it.

### Secret handling

- Never log cookies, tokens, or request bodies matching common secret patterns (Bearer tokens, API keys, JWTs)
- Pattern list lives in `core/config.py` as `REDACTION_PATTERNS`
- Redaction applied to all log lines that might contain request bodies or headers — verified by a test

### Post-v0 auth

When the API is added: API keys (Bearer in `Authorization` header), hashed in DB, scoped to org. OAuth (Google, GitHub) in v1. Stripe and webhook secrets encrypted at rest.

---

## When in doubt

- If a feature seems to belong to v0 but isn't listed, it's post-v0 or v1. Confirm before implementing.
- If a probe needs framework knowledge, the adapter contract needs extending — not the probe.
- If static analysis is uncertain, broaden scope and tell the user. Never narrow silently.
- If a test would require mocking the browser, it's the wrong layer of test.
- If you find yourself reaching for a new dependency, check if Python stdlib or an existing dep covers it.
- If a wrapper concern is leaking into the core, the abstraction is wrong — fix the boundary, not the symptom.
- If a query touches user data and doesn't filter by `org_id`, it's a security bug regardless of whether tenancy is "used" yet.
- If a CLI command does something a future API endpoint will also do, write the logic in `db/repository.py` and have both call it — never duplicate.

The North Star is: **the user should trust this tool's silence**. A clean Postcheck report should mean the change is genuinely verified, not just that the tool didn't look hard enough. Every design decision serves that.