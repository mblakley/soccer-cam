Show pipeline completion status for every game through all sequential stages.

```bash
uv run python -m training.pipeline.progress $ARGUMENTS
```

Common usage:
- `/progress` — all games
- `/progress --team flash` — flash games only
- `/progress --team heat` — heat games only
- `/progress --incomplete` — only show games missing stages
- `/progress --game flash__2024.06.30_vs_IYSA_away` — single game

Each game shows a progress bar: Staged > Tiled > Labeled > QA > Phases > Field > Ready

Green = complete, gray = pending. Summarize which games are fully complete and which need work.
