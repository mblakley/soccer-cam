Run a thorough end-to-end verification of the application:

1. Lint: `uv run ruff check`
2. Format: `uv run ruff format --check`
3. Unit tests: `uv run pytest -m "not integration and not e2e" -v`
4. Integration tests: `uv run pytest -m "integration" -v`
5. Verify the app can start without errors: Check that imports resolve and config loads

Report pass/fail for each step. If any step fails, investigate the root cause and fix it.
