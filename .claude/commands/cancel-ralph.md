Cancel the active Ralph loop by deleting the state file at `.claude/ralph-state.json`.

1. Check if `.claude/ralph-state.json` exists
2. If it exists, read it first to report the current iteration count, then delete it
3. If it doesn't exist, inform the user that no Ralph loop is currently active

Confirm the cancellation to the user.
