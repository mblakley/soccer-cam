# Development setup

From a fresh clone, two commands:

```bash
uv sync          # everything needed to lint, format and run the tests
uv run pytest -m "not integration and not e2e"
```

`uv sync` with no flags is enough. The tooling lives in `[dependency-groups] dev`,
which uv installs by default. The optional extras are for things not everyone
needs:

| Extra | Adds | When you need it |
|---|---|---|
| `--extra tray` | PyQt6, pywin32 | Working on the Windows tray icon |
| `--extra service` | pywin32 | Working on the Windows service wrapper |
| `--extra dev` | PyInstaller, Cython | Building installers |

## Verifying a change

```bash
uv run ruff check                                  # lint
uv run ruff format --check                         # formatting
uv run pytest -m "not integration and not e2e"     # unit tests
```

All three must pass before you commit. `/verify` runs them together.

## Test suites and how long they take

| Command | Tests | Roughly |
|---|---|---|
| `uv run pytest tests/web/test_design_system.py` | 31 | 4s |
| `uv run pytest tests/web/` | ~200 | 1m |
| `uv run pytest -m "not integration and not e2e"` | ~2000 | 8m |

Start with the design-system guards when you touch the UI — they are the fastest
signal and they catch the mistakes that are easiest to make.

If the whole suite feels slower than the table above, check whether something
started building a fresh TLS context per test. `TTTApiClient` shares one via
`_shared_ssl_context()`; without it, `ssl.load_verify_locations` cost ~2s per
`create_app` and dominated the run.

Integration and e2e tests need real hardware or a live server:

```bash
uv run pytest -m integration
```

## Looking at the UI

You do not need a camera, a config file, or a running pipeline:

```bash
uv run python -m video_grouper.web.preview
```

That serves an index on `127.0.0.1:8799` grouping what it renders:

- **Routes** — the pages the orchestrator actually serves, labelled with the
  path each one lives at (`/`, `/config`, `/setup/camera`, `/stitch`)
- **States** — those same pages in conditions that are awkward to reproduce on
  demand, like a failed save or a rejected sign-in
- **Tally swatches** — not pages; one per capture state so the four are
  comparable side by side

The topbar links are rewritten to the rendered files, so the preview is
browsable rather than 404-ing on `/config`. `--out DIR` writes the files
instead of serving; `--port N` picks the port.

To look at it from a phone, put it on the tailnet:

```bash
tailscale serve --bg --http=8799 http://localhost:8799
# -> http://<this-machine>.<tailnet>.ts.net:8799/
tailscale serve --http=8799 off      # when done
```

`serve` is tailnet-only. Do not use `funnel`, which publishes to the internet.

Use it whenever you change anything visual. It is much faster than starting the
orchestrator, and it shows states (a failed save, a disconnected camera, a live
recording) that are awkward to reproduce for real.

## Changing the UI

The design system is documented in [UI-DESIGN.md](UI-DESIGN.md). The short
version:

- Styling goes in `video_grouper/web/static/soccer-cam.css`. One file.
- Pages are assembled from `video_grouper/web/chrome.py`, never hand-rolled.
- `tests/web/test_design_system.py` enforces both. Run it first.

If a guard fails, it is usually right — read the assertion message before
changing the test.

## Running the app

```bash
uv run python run.py                    # orchestrator + web UI on :8765
uv run python -m video_grouper.tray     # Windows tray icon
```
