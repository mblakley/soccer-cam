Run the full verification suite for this project:

1. Run ruff linting: `uv run ruff check`
2. Run ruff formatting check: `uv run ruff format --check`
3. Run all unit tests (skip integration/e2e): `uv run pytest -m "not integration and not e2e" -v`

Report a summary of results. If anything fails, diagnose the issue and suggest (but don't apply) fixes.
