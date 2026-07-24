# Labeling Log

Mark's clicks are the scarcest resource in the system (DECISIONS 2026-07-23 (e)). Every
batch: what was asked, est. vs actual clicks, and WHAT IT DECIDED. Future asks are ranked
on this exchange-rate evidence.

| date | batch | ~clicks | consumer | what it decided |
|---|---|---|---|---|
| ≤07-18 | spc_* far-label sets (SPC held-out GT) | ~1,800 rows | detector G1 evals since EXP-DIST-46 | every detector verdict 46→69; frozen as benchmark v1 |
| ≤07-19 | wind gust sets (seg7/11) + windy Fairport | ~380 | EXP-DIST-57/63 wind arc | stabilization = opt-in no-op at moderate wind; tail test |
| 07-19 | spc/fair_viewport_worst (700+706 views) | ~1,400 views (interp-assisted) | EXP-DIST-62 viewport benchmark | the PRODUCT metric; we crush AutoCam; frozen v1 |
| 07-22 | field-polygon confirms (39 games, field editor) | ~39 confirms + drags | geometry-conditioned detector (EXP-DIST-66→) | clean polygon store; geometry descriptor; distance policy |
| 07-23 | **Pittsford Dahua viewport sets** (div-sampled 500 + cold-audit 60) | 560 views in ~18 min (A/C-confirm 154, hand-placed ~406) | Phase 2 verdict: the ONLY human-GT cross-camera read + seed-miss correction | labeled same evening as READY; cold audit: AC confirmed on 50/60 random active frames → AC is a credible Dahua reference; extension delta (blind arm-disagreement frames) still pending |
| pending | event-spreading tail queues (30–50 fr × 6–8 games) | est. 250–400 | per-game noise bands + eval event tripling | — |
| deferred | aerial | 0 | none — no aerial experiment exists | — |
