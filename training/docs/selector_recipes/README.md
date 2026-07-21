# Selector training recipes (preserved server-side orchestration)

These are the **canonical orchestration scripts** that produced the shipped ball-selector
models (`selector_v5/v6/v7.npz`). They ran on the training server (paths reference `F:`/`G:`
server drives and `D:\training_data\far_label`); they are preserved here verbatim (only the
personal ntfy push endpoint scrubbed to an env var) so the champion recipe is never lost —
they were server-only until 2026-07-21, which is exactly how a champion model's recipe gets
orphaned. Treat them as executable documentation, not repo CLIs; the real work is in the
committed CLIs they call (`build_selector_labels`, `kill_test_selector`, `export_ball_selector`).

| script | model | what it does |
|---|---|---|
| `v5v6.py` | v5 → v6 | 15-game marathon labels → train → export |
| `overnight_selector.py` | v6 (canonical) | consolidate `__near_close` far-label sets into `ball_labels.jsonl`, rebuild selector labels, train, export |
| `overnight_selector_v7.py` | **v7 (current champion)** | as v6 **plus `__mid` consolidation**; STEP 0 folds near_close+mid human labels into per-game `ball_labels.jsonl` (backing up `.bak_v7`), STEP 1 rebuilds `sel_labels_*_v7.json` from the augmented labels with `--gold-weight 20.0`, STEP 2 `kill_test_selector --save-net`, STEP 3 export |
| `band_diag.py` | — | per-depth-band held-out diagnostic (near/mid/far) used to score v6/v7 |

## Why v8 (EXP-DIST-64) did NOT reproduce v7

v8 reused the frozen `sel_labels_*_v7.json` files directly instead of re-running STEP 0+1.
**Checked (2026-07-21): the dumps are NOT stale** — the `fullgame/<game>` dump `meta.json`
mtimes are 07-04..08, *predating* the 07-12 labels, so the `ef`-index → candidate mapping is
consistent (my first guess — a re-dump — was wrong).

The actual gaps, both real:
1. **v8 trained on 14 of v7's 15 games** — `heat__2026.05.07_vs_Pittsford_Mustangs_away_18.28`'s
   fullgame dump was cleared (0 parts), so it was dropped. v7 trained on all 15.
2. **The selector is seed/val-split sensitive**: `train_selector` does a random val split for
   early-stop AND a temperature grid-search; temperature sets the softmax sharpness → the `pnone`
   the tracker consumes as its miss-cost. A different training set (14 vs 15 games) shifts the
   split → temperature → `pnone` calibration, and the product-chain FAR is very sensitive to
   `pnone` (v8_db00 far collapsed to 0.174 vs v7 0.722 through the tracker, while its per-frame
   learned-argmax far was a healthy 0.373 — i.e. the net is fine, its miss-state calibration is off).

**A faithful v8/selector retrain must:** re-dump the missing pittsford0507 game, re-run
`build_selector_labels` against the current dumps (STEP 1), train on all 15, and report the
temperature so `pnone` calibration is comparable run-to-run.
