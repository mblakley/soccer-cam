Run the pipeline data integrity audit.

```bash
uv run python -m training.pipeline.audit $ARGUMENTS
```

Common usage:
- `/audit` — full audit (skips games that already passed and haven't changed)
- `/audit --force` — re-check everything, even previously clean games
- `/audit --game flash__2024.06.30_vs_IYSA_away` — audit a single game

Review the output and summarize the findings grouped by severity (CRITICAL, WARNING, CLEAN). For any CRITICAL issues, recommend the fix command shown in the output.
