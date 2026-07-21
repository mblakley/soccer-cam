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

The v8 recalibration reused the frozen `sel_labels_*_v7.json` files directly instead of
re-running STEP 0+1. If the `fullgame/<game>` candidate dumps were re-dumped after 2026-07-12
(when the v7 labels were built), the labels' `ef`-index → candidate mapping no longer aligns
with the current dump content — training on mismatched (label, candidate) pairs. A faithful
v8 must re-run `build_selector_labels` against the CURRENT dumps (as v7 STEP 1 does), not reuse
stale label files. Verify dump vs label freshness before any selector retrain.
