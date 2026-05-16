# Postcheck

Postcheck is a **post-completion verification system** that runs after an LLM finishes a coding task and the code compiles. It does what the LLM does not: exercise the changed code in a real browser, catch runtime errors, network failures, storage issues, and UI regressions, and report them back as a structured bug list. If the user then asks for a fix, a separate agent attempts it.

## Quickstart

```bash
git clone <repo-url> postcheck
cd postcheck

# Install Python deps + Playwright Chromium
make install

# Bring up Postgres + Redis + API + worker
make dev

# Run a verification against an example
postcheck verify --sync
```

Postcheck attaches to a Chrome instance you control via the DevTools Protocol. On first run, launch Chrome with:

```bash
chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.postcheck-chrome-profile
```

Then log in to your local app once. Postcheck will reuse that session.

## Common make targets

| target           | purpose                                               |
| ---------------- | ----------------------------------------------------- |
| `make install`   | install package + dev deps + Playwright Chromium      |
| `make dev`       | `docker compose up -d` (Postgres, Redis, API, worker) |
| `make down`      | stop the dev stack                                    |
| `make test`      | run pytest                                            |
| `make lint`      | ruff check                                            |
| `make fmt`       | ruff format + autofix                                 |
| `make typecheck` | mypy --strict                                         |
| `make migrate`   | alembic upgrade head                                  |

## Contributing

Read [CLAUDE.md](CLAUDE.md) before opening a PR. It is the source of truth for architecture, v0/v1 scope boundaries, conventions, and module specifications.
