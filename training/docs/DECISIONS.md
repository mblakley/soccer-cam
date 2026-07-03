# Decision Log

Append-only. Never delete entries — if a decision is reversed, add a new entry explaining why.

---

## 2026-07-02: Decode raw per-segment clips, not the combined video, for crop/strip extraction

**Context.** The per-game `combined.mp4` is a stream-copy concat of the raw camera segments with
realigned audio — it's VFR, slow to decode end-to-end, and one corrupt packet crashes the whole decode
(the Flaitz game produced no far-label set). Verified the combined's frame `offset+f` is bit-identical
to raw-segment frame `f` (0.00 diff, incl. cross-segment).
**Decision.** Extract wanted frames from the **raw per-segment clips** via `data_prep/segment_decode`
(`global = segment.global_offset + f`; GOP=20 / VFR-aware keyframe-seek + presentation-order PTS
matching). Frame-exact, corruption-isolated per segment, and fast (decode ~one GOP per label). Applies
to `build_far_label_queue` and `build_human_crops`; replaces the earlier stop-at-corruption stopgap.
**Trade-off.** Building the per-segment PTS map demuxes the whole clip (I/O), so the win is avoiding
the full *decode*, not the full read — still ~an order of magnitude less decode work than sequential.

## 2026-06-30: Distill AutoCam by feeding its detections to OUR existing tracker — teacher is the tracked ball, not the viewport; train a detector, not a viewport model

**The decision.** To beat AutoCam on far balls, we train OUR ball **detector** (HeatmapNet) and run
it through OUR **existing** tracker (`world_model.reranker.track_ball`) → viewport. We do **not**
train a viewport/end-to-end model, and we do **not** distill AutoCam's smoothed `autocam_viewport`
(camera-centre). The distillation **teacher label** each frame is the ball position produced by
running the existing tracker over AutoCam's re-run per-frame detections (`autocam_detections.jsonl`),
anchored by human GT and snapped to the real in-field detection (`teacher_track`).

**Why (measured, EXP-DIST-16).** On 1,880 human far-GT balls (frames AutoCam loses): ball present in
AutoCam's detection candidate set **0.97**; existing tracker over those detections **R15 m 0.77**
(median 2.1 m); AutoCam's own viewport **0.15** (median 41 m); AutoCam's raw argmax/viewport-gated
pick **0.10**. So the detector *finds* the far ball AutoCam loses — the gap is **selection**, which
the existing tracker already closes. The viewport-vs-GT error is random-direction (AutoCam looks
elsewhere), not a coordinate artifact. Distilling the viewport would cap us at AutoCam and can't win.

**Corollaries.** (a) Teacher restricted to **active play** (game_state halves; warm-up/halftime
AutoCam tracks players) and **in-field** (marathon detections carry off-field FPs); human far-GT is
exempt and always kept. (b) **Reolink-primary**: build the first detector on Reolink (clean AutoCam
detection + GT-anchored); 2024 Dahua games have noisy detection and no GT — fold in down-weighted
later. (c) The success bar is Mark's: "close enough that the viewport can follow the ball" = meters
at R10–15, matching AutoCam in normal play + beating it on far vs human GT.

**Reverses the prior session's plan** (world-model re-ranker over a sparse-far-label detector; the
"far-weight" loss up-weighting, EXP-DIST-13/14, which hurt). See also
[[reference_autocam_ball_detections]], [[project_tracker_selection_finding]].

---

## 2026-06-26: `orientation` is RAW-camera provenance, NOT a flip instruction — derive flip from the resolved video's rotation tag

**The trap:** the registry/`game.json` `orientation` field (12 `upside_down`, 91 right-side-up) describes how the
**raw camera was mounted** — it does NOT mean "the video you'll decode is upside-down." Two different corrections exist
in the corpus, both yielding right-side-up frames when read correctly:
- **Tag method (7-10 of the upside_down games):** upside-down pixels + a **`rotation=-180` displaymatrix tag**, as a
  stream-COPY `<slug>-raw.mp4` (no re-encode). **cv2 auto-applies** the tag → right-side-up. **PyAV/libav IGNORE
  display-rotation by default** → upside-down unless the reader applies it.
- **Baked method (e.g. `flash__2025.05.17_vs_NY_Rush_away`, `heat__2025.06.02_vs_Fairport_home`):** the flip is baked
  into the **pixels**, NO tag. Every reader gets right-side-up. **These were vision-confirmed right-side-up — flipping
  them (adding a -180 tag) would INVERT them.** (Their `needs_flip=True` registry flag was stale-for-processing.)

**RULE (so no session naively flips on the `orientation` field):**
1. **Never flip based on `orientation` alone.** Derive the action from the **resolved video's actual rotation tag**.
2. `game.json.video_rotation` = the resolved (`resolve_source`) video's display-rotation (`-180` for tag-corrected
   games, `0` otherwise incl. baked + natively-upright). **cv2 consumers: do nothing** (auto-applied). **PyAV / any
   non-cv2 reader: apply `video_rotation`** (rotate 180° if `-180`), or its frames will be upside-down vs the
   right-side-up labels/polygon.
3. `needs_flip` is DROPPED (was `upside_down ∧ no corrected_video`, stale-prone, and described the raw not the
   processed video). `orientation` stays as provenance only.

**Load-bearing follow-up:** the crop/dewarp builder (PyAV) MUST honor `video_rotation` — otherwise its crops for the
~10 tag-corrected games come out upside-down vs their right-side-up labels (silent train-data corruption). Verify before
the next training run. See [[feedback_verify_label_semantics_with_vision]].

## 2026-06-26: Human far-labels were made on DIFFERENT source videos than the detections — align by frame-matched offset

**The trap (so no future session re-derives it):** the 27 `D:\training_data\far_label\<set>\` sets were NOT all
labeled on the same video the marathon/detections use. Their `manifest.json` `clip` field points at whatever the
annotator opened, which is one of FOUR source kinds — each needs different handling to land on the canonical `(seg,f)`:

| clip kind | example | how `frame_idx` maps to `(seg,f)` |
|---|---|---|
| **per-segment** | a single `RecM09_*.mp4` in the F: game dir | `seg` = clip stem, `f` = `frame_idx` (no offset) |
| **full combined** | `D:\soccer-cam-storage\<date>\combined.mp4` (== F: `combined.mp4`, same frame count) | `frame_idx` → offset table directly (offset 0). e.g. Pittsford `__queue` |
| **start-TRIMMED raw** | `D:\soccer-cam-storage\<date>\...raw.mp4` (FEWER frames than F: combined) | `frame_idx + OFFSET` → offset table. **constant per-game start-trim** |
| **derived test-clip** | `D:\detect_work\v4_test_clips\*.mp4` (small contiguous extract) | frame-match the extract into the combined → source start; `frame_idx + start` |

**Canonical frame basis = the registry-resolved `F:\...\<game>\combined.mp4`** (== concat of the RecM09 segments;
`game.json` `segments[].global_offset` is built from it). The marathon (`resolve_source` → `kind=combined`) and all
detections use it. **A label clip's frame numbering must be aligned to THIS before trusting `(seg,f)`.**

**Alignment method (proven, automated in `build_ball_labels.py:raw_to_combined_offset`):** frame-match the label clip
against the combined — downscaled (160×90) grayscale mean-abs-diff, swept over candidate offsets; a true match reads
**diff ≈ 0.0 (pixel-identical)** and the offset is **constant across the whole game (no drift)** → a single integer
recovers every label. **Vision-verify** at least one recovered label lands on the ball (HARD GATE — this caught the
original mis-map: markers on grass via the wrong basis vs on the ball via the right one). See
[[feedback_verify_label_semantics_with_vision]].

**Per-game offsets computed (raw `frame_idx` → combined frame):**
| game | label source | offset | source frames | combined frames |
|---|---|---|---|---|
| `heat__2026.05.31_vs_Spencerport_gold_2_away` | `...spencerport-gold-2...-raw.mp4` (trimmed) | **+1162** | 80,640 | 81,800 |
| `heat__2026.06.15_vs_Irondequoit_away` (`heat_0615_*` sets) | trimmed raw | **+1181** | — | 109,340 |
| `heat__2026.05.07_vs_Pittsford_Mustangs_away_18.28` (`__queue`) | full `combined.mp4` | **~0** | 113,460 | 113,460 |
| `irondequoit` test-clip set | `v4_test_clips\irondequoit_far_*.mp4` (762-frame extract) | frame-matched start (pending) | 762 | 109,340 |

These offsets are also written into each game's `ball_labels.jsonl` `_meta` header (per-set provenance: `source_clip`,
`basis`, `offset`) so the data is self-describing. Recompute via the frame-match if a video is ever re-encoded.

## 2026-06-26: Frame indexing = canonical (seg,f) + derived global_offset; corruption handled via corrupt[] ranges + one decode policy

**Context:** Producers currently disagree on frame keys — the #27 marathon keys detections by a running **global**
frame; `gamedata` `labels.jsonl`/`track.jsonl` key by **(seg, f)** per-segment; AutoCam's sidecar + `viewport.json`
are global. Mark: store an overall frame offset so it's consistent, and handle the corrupt-frame case (the
`heat__2026.06.08_vs_Hilton_Heat_Flaitz_away` reolink game that crash-looped the curve's crop builder).

**Minimal investigation (06.08, the one confirmed-corrupt game):** 25 raw reolink segments + a 18.9 GB `combined.mp4`.
Demux packet count == header frame count **exactly on all 25 segments AND the combined (143,800), with zero demux
errors**. So: no truncation, no missing packets, and `combined = Σ segments` holds precisely. **The corruption does
NOT drop frames from the stream** — it is decode-level (a concealable bad frame) or, more likely per the prior note,
an OOM in the crop builder (pixel accumulation on the 16 GB box), i.e. NOT data loss. (Stopped the full triple-decode
classification as non-essential — it does not change the schema.)

**Decision:**
1. **Canonical key = `(seg, f)`** — `seg` = segment filename stem, `f` = 0-based decode index within that segment.
   Per-segment indexing isolates corruption (a bad frame in segment k cannot shift indices in k+1..N) and works with
   or without a physical `combined.mp4`. Matches the existing `gamedata` schema.
2. **Global frame is DERIVED, not stored per record:** `game.json` `segments[]` carries `frames` (actual decoded
   count) + `global_offset` (cumulative prior decoded frames, in registry segment order). `global_frame =
   global_offset[seg] + f`. Recomputable from one corrected count without rewriting any measurement record. (06.08
   shows header==packets==offset, so offsets are trustworthy; build them from the canonical decode, never header alone.)
3. **Corruption handling = `corrupt` ranges per segment** in `game.json` (`"corrupt":[[f_start,f_end],...]`, empty
   when clean), populated by the canonical decode pass (ffmpeg `-v error` / PyAV error-scan). Consumers skip those
   frame ranges for labels/training. No PTS/`t` field needed **because frame count is preserved through concealment**
   — if a future game ever shows decoders disagreeing on COUNT (mid-stream frame *drops*, not just bad pixels), revisit
   by adding a `t` (PTS seconds) anchor.
4. **One canonical, error-tolerant, count-preserving decode policy** for ALL producers (marathon/cv2, crop-builder/PyAV,
   viewport indexer) so `(seg, f)` denotes the same frame everywhere even through a corrupt spot. The 06.08 curve crash
   was a crop-builder OOM, fixed by streaming/memory limits — separate from indexing.

**Data-shape primitives (locked):** point `[x,y]` · detection `[x,y,conf]` · **viewport `[cx,cy,w,h]`** (center+size:
pan=cx,cy, zoom=w,h — matches how the broadcast crop is parameterized) · polygon `[[x,y],…]`. All source px in the
segment's native resolution (declared in `game.json`). Per-frame record envelope: `{"seg","f", …}` (+ `p`/`d`/`v`).

## 2026-06-26: Per-game measurement store = JSONL sidecars next to the video on F: (retire manifest.db)

**Context (Mark):** "I only want to maintain the jsonl file, not the manifest db — the sqlite DBs were
fragile, and were only necessary when we were using the tiling approach." + "The F drive archive is the most
important — the sidecars 100% need to end up there." + "write these sidecar files next to the video files, not
in some other random directory where they will get lost." We had accumulated 4+ parallel stores for the same
measurements (`F:\autocam_data\<gid>\detections.json`+`viewport.json` whole-file JSON; `F:\gamedata\<gid>\`
keyed-dir `labels.jsonl`/`track.jsonl`; `D:\training_data\far_label\…\labels.json` human labels on the
**transient** D: tier; `manifest.db` ball tables) — fragmented, some off the F: archive, easy to orphan.

**Verified facts:** all 103 registry games have their `path` (video dir) **on F:** and present on disk
(`F:\Flash_2013s\<game>\`, `F:\Heat_2012s\<game>\`). AutoCam already writes its sidecar next to the video as
`<processed>.mp4.jsonl`. The existing `gamedata` store already uses a clean per-segment jsonl schema
`{"seg","f","x","y","qa"}` (`labels.jsonl`) / `{"seg","f","x","y"}` (`track.jsonl`) read by `gamedata.py` (`gd`).

**Decision:** ONE canonical store — **append-only JSONL sidecars written into each game's own video dir on F:**
(`<registry path>`), beside the segment `.mp4`s. **No SQLite/manifest.db going forward** (legacy DBs become
derived/droppable; they existed only for the retired tiling pipeline). Per-game files:

| file | content | schema (one record per line) |
|---|---|---|
| `game.json` | meta (NOT jsonl) | game_id, team, camera, segments[], w/h/fps, field polygon, play_windows, schema_version |
| `ball_labels.jsonl` | human GT + visibility (**precious**) | `{"seg","f","x","y","a","src","set","ts"}`  a∈ball\|not_visible\|out_of_play |
| `ball_track.jsonl` | canonical selected ball path | `{"seg","f","x","y"}` |
| `autocam_detections.jsonl` | AutoCam raw balldet candidates | `{"seg","f","x","y","conf"}` (1 row/candidate) |
| `autocam_viewport.jsonl` | AutoCam camera center | `{"seg","f","x","y"}` |
| `homegrown_detections.jsonl` | (future) our detector | `{"seg","f","x","y","conf","model"}` |
| `homegrown_viewport.jsonl` | (future) our camera center | `{"seg","f","x","y","model"}` |

Naming pattern: `<producer>_<measurement>.jsonl` (producer ∈ autocam\|homegrown, measurement ∈ detections\|viewport)
+ producer-neutral ground-truth (`ball_labels`=human, `ball_track`=canonical). Adding a producer/measurement later
= a new file, zero migration. Keep the existing `{seg,f,x,y}` **per-segment** frame indexing (matches the
multi-segment reality; no fragile global-frame mapping). Coords = that segment's native source px (declared in
`game.json`). Each `.jsonl` opens with a `{"_meta":{schema_version,units,frame_base}}` header line so it is
self-describing standalone.

**Why:** consistent (same files/schema every game) · resilient/safe (append-only — a crash loses ≤1 line, no
SQLite corruption, no whole-file rewrites like `detections.json`) · easy-read (one JSON/line, grep/tail) ·
easy-parse (`for line in f: json.loads`, or `gd`) · future-proof. Co-located with the video so a measurement can
never be orphaned from its source; 100% on the F: permanent archive.

**Migration (incremental, additive-first):** (1) switch the running #27 marathon to append
`autocam_detections.jsonl` into each video dir (also removes the whole-file-rewrite risk for the 3-4 day run);
(2) convert the 61 `viewport.json` → `autocam_viewport.jsonl`; (3) consolidate the 27 `D:\far_label` sets →
per-game `ball_labels.jsonl` (carry `src`/`set`/`ts` provenance — gets the precious human labels off transient D:);
(4) migrate `F:\gamedata\` `labels`/`track` jsonl next to the video and **repoint `gamedata.py` + DATA_INVENTORY.md**;
(5) stop writing measurements to manifest.db. Do additive writes first; defer deletions/repointing until verified.

**Trade-off / open:** layout is **flat files in the video dir** (matches the AutoCam `.mp4.jsonl` precedent) rather
than a `measurements\` subdir. Retiring manifest.db is a go-forward stance for the *measurement streams*; a full
sweep of all manifest.db readers/writers (field_boundary, game_phases) is a separate follow-up, not this change.

## 2026-06-26: Split preprocessing — AutoCam geometry for the teacher labels, our dewarp for the student, meters for eval

**Context:** This session proved AutoCam's exact balldet input recipe (full-frame, isotropic-to-`max_width=1600`
+ ceil-pad-to-32, RGB/255/fp32, **no crop / no ROI / no tracking gate**) and, separately, that anisotropic
distortion silently corrupts labels (the marathon's hardcoded `1600x448` squished 4096x1800 Dahua ~36% vertically
→ ~16% of confident top-1 detections locked onto the WRONG object; EXP-DIST-15). Two preprocessing goals are in
tension: (a) **match AutoCam exactly** for a clean apples-to-apples comparison and a faithful distill teacher, vs
(b) **use our own perspective-normalized dewarp** to give the detector a more consistent/larger ball than
AutoCam's raw global downscale (~4.7px Dahua ball at 1600-wide) — which is exactly where we already beat AutoCam
on far balls (0.37 vs 0.11).

**Decision (Mark, 2026-06-26):** Do not pick one preprocessing; **split the three stages so each uses the right
one:**
1. **Teacher-label generation** (the AutoCam distill source) runs at **AutoCam's EXACT geometry** — per-game
   isotropic-resize-to-1600-wide + ceil-pad-height-to-32 + bottom-zero-pad + RGB/255/fp32 (480 @ 7680x2160,
   704 @ 4096x1800). This makes the distilled labels a faithful reproduction of AutoCam's own ball decisions and
   keeps the head-to-head honest. (Now implemented in `gen_detections_all.py`.)
2. **Student training** runs on **our own perspective-normalized / dewarped crops** with **isotropic** per-camera
   ball-scale normalization — NOT AutoCam's raw downscale. The student may see a better-conditioned ball than the
   teacher did; that's the point (it's how we beat AutoCam on far/lost balls). Map teacher labels into the
   student's space carefully; never reuse a non-isotropic resize anywhere in this path.
3. **Evaluation** is scored **in meters on the field plane** (`geom.image_to_world`), which is invariant to either
   side's pixel preprocessing — so preprocessing differences cannot bias the AutoCam-vs-ours comparison. Divergence
   frames get human labels; AutoCam is never GT where we disagree with it.

**Why:** Matching AutoCam's geometry and using our own better geometry pull in opposite directions only if you
force one preprocessing across all three stages. Separating them gets both: a faithful, low-noise teacher AND a
stronger student, with a comparison that can't be gamed by preprocessing. The squish bug is the concrete proof
that label-gen fidelity (stage 1) and student conditioning (stage 2) are distinct concerns that must be handled
separately — a single shared resize silently broke 16% of Dahua labels.

**Trade-offs / guardrails:**
- **Isotropic everywhere is a hard rule now** — any aspect-distorting resize is banned in all three stages
  (validated by EXP-DIST-15). Per-camera normalization must scale, never squish.
- Distillation gets us to **parity** in normal play (the part we were failing); we only **beat** AutoCam by
  grafting human far-ball labels onto the frames it loses — distill-alone ≠ winning. The split does not change
  that; it just stops preprocessing from being a confound.
- Shipped-model export stays **fixed-shape fp32** (ORT graph-fusion / DML-iGPU efficiency; Pascal fp16 is crippled)
  — orthogonal to the training-preprocessing split but part of the same "geometry/shape discipline" lesson.

## 2026-06-25: Halt the data-scaling curve at 4 rows; pivot to the recall experiment; defer N=16

**Context:** The distill data-scaling curve completed N=1/2/4/8 (4 rows) and those already answer its question —
more games on the current recipe does not improve recall (HARD R15 bounces 0.39/0.22/0.36/0.33; NORMAL R15
flat-to-regressed 0.155/0.081/0.153/0.009). The final N=16 row then entered a crash-loop: the orchestrator
relaunched `iter_run` ~25× over 2h, each failing rc=1 with **no Python traceback**, dying deterministically while
building crops for one game (`guzzetta__2026.06.08_vs_Hilton_Heat_Flaitz`). No-traceback + native = a PyAV decode
segfault (`0xC0000005`, the known issue on this box) or a 16 GB-RAM OOM on that video — environmental, not a code
bug. G: had 20 GB free, so not disk.

**Decision (Mark, 2026-06-25):**
- **Accept the 4-row curve as the scaling answer and halt the orchestrator** (done; `curve.jsonl` preserved).
- **Pivot the GPU to the staged recall experiment (EXP-DIST-13) immediately.** A 5th point on an already-flat line,
  gated behind debugging one game's native decode crash, is near-zero value; the recall recipe-change is the lever.
- **Debug the 06.08 video in a parallel CPU sub-agent** (decode triage / re-encode), and **run N=16 AFTER the
  recall experiment finishes**, with the fixed video — not before. The recall run does not include 06.08, so it is
  unaffected by the crash.

**Why:** This is the second time the curve-vs-recall GPU question arose; the first answer ("finish curve first")
was premised on the curve being able to finish cheaply, which the native crash falsified. The curve's purpose
(measure whether data scaling fixes recall) is fulfilled by the 4 rows; chasing the 5th costs hours for no new
signal. Deferring rather than abandoning N=16 keeps the curve completable once the video is fixed.

**Trade-off:** The curve headline is 4 points not 5 until N=16 reruns; acceptable since the trend is unambiguous
and N=16 is queued, not dropped. crops_game (the curve's 14 built games) is **retained** specifically so the
deferred N=16 reuses it rather than rebuilding 2h of crops.

## 2026-06-24: Stop selection engineering — the far-ball gap is detector-recall-bound, not tracker-bound

**Context:** A focused 5-experiment chain (EXP-DIST-08..12, all CPU-only, curve undisturbed) re-scored the
distill detector + `track_ball` against AutoCam on clean human GT and decomposed the loss. Findings: the
"~5× worse than AutoCam" headline was a **far-third measurement artifact** (the held-out GT was entirely the
far sliver, and the AutoCam baseline was circular). On clean GT the honest AutoCam viewport is 0.748, and
per-band selection success is 0.27 far / 0.57 mid / 0.71 near — **near/mid argmax is already ≈ AutoCam before
any tracking**. The only hole is the far third. Far is *not* a tracker problem: our shipped meters-Viterbi/
Kalman adds nothing on far (0.153→0.153) and its teleport gate hard-forbids the correct far candidate 82.5%
of the time (far m/px is 0.09–0.19, so a correct far move looks like a 67 m teleport). The reference tool's
far-follow has **no selection intelligence** — argmax → ~3 s recency-weighted pixel-space moving average, no
rejection. Replicating that dumb smoother **doubles** our far recall (0.153→0.342) but caps there: an oracle
*perfect* per-frame selector = 0.811 un-smoothed but collapses to 0.369 through the same smoother (smoothing
is a ~16–19 m floor), and our detector's far argmax is only 13.5% ball-centered.

**Decision:**
- **No more selection/tracker engineering as the path to the far gap.** It is characterized and near its
  ceiling. The dominant remaining wall is **detector far-RECALL** — getting the ball to be the argmax — which
  the venue-diversity data-scaling curve and the new far-label sets directly target. That is the primary lever.
- **Bank, but do not yet ship, the far-band simplification:** replace the far-band selector with the dumb
  pixel-smoother (or minimally drop the teleport gate behind a far-band flag) for a cheap ~2.2× far win. It is
  **gated on a near/mid no-regression A/B**, which needs a continuous near/mid candidate stream (GPU/decode →
  post-curve). The shipped `reranker.py` default stays untouched until that check passes. Geometric evidence
  (the fix scales the budget by local m/px, which collapses 4–15× from far to near with a `max(scale,1)`
  guard) says it cannot loosen near/mid association — but we verify empirically before shipping.
- **Open a post-curve detector experiment:** test whether matching the reference detector's operating input
  width raises our far argmax (resolve the conflicting RE width notes in the F: archive first).

**Why:** The decomposition is unambiguous — every downstream selector/smoother is capped by how often the ball
is the per-frame argmax, and ours is 13.5% on far. Spending more on selection chases a ~0.06–0.19 residual
while the detector caps the whole system. This also corrects the prior session's framing that "the tracker
beats AutoCam" (that 0.58 was 5 cherry-picked clips; on continuous far play the same tracker is 0.153).

**Trade-off:** We leave a real ~2.2× far-selection win on the bench until the near/mid regression check is
possible. Acceptable: shipping an unverified change to the live broadcast selector risks regressing the 70%
of play (near/mid) that already works, and the detector lever dwarfs it anyway.

## 2026-06-24: 6/15 active-play windows go in the canonical `play_windows.json` at TRUE fps, keyed by archive id

**Context:** Mark gave exact active-play windows for `heat__2026.06.15_vs_Irondequoit_away`. Two ambiguities
had to be resolved when persisting them: (a) **which store/key**, and (b) **which fps** to convert
seconds→frames with.

**Decision:**
- **Store = the existing `G:\ballresearch\play_windows.json`** — the canonical active-play store for 2026
  games (which have no per-game `manifest.db`; 2024/25 games carry their phases in `manifest.db`
  `game_phases`). It is read by the standard accessor `gamedata_sources.play_windows()` and directly by the
  distill pipeline (`iter_run.py` crop gate, `orchestrator.py` curve eligibility). No new side-file was
  created — that would have violated the "one canonical store, never invent side-files" rule.
- **Key = `guzzetta__2026.06.15_vs_Irondequoit`** (the `ball_distill` archive-dir name), NOT the registry id
  `heat__…`. Every existing entry is keyed by the archive-dir name, and `iter_run.py`/`orchestrator.py` do an
  **exact-key** lookup on that name. Heat archives carry the legacy `guzzetta__` prefix. The registry-id
  fallback in `gamedata_sources.play_windows()` (token-overlap ≥4) still resolves `heat__…away` to this entry
  unambiguously (overlap 5; the 6/04 sibling is overlap 3 and has `windows:null`).
- **fps = the video's TRUE 19.815** (measured `average_rate=19.8149`, 108160 frames / 5458.5 s), NOT the
  integer `FPS=20` proxy `add_play_windows.py` uses for the other 2026 entries. At 91 minutes the proxy drifts
  ~1% (~50 frames by game-end) — fine as a coarse warmup/halftime cut, but Mark supplied precise frames so we
  store them precisely. These raw-frame indices equal the archive global-frame space (concat of the 19
  segments), so they need no conversion for the `iter_run.py` `base+f` consumer.

**Why:** Match the existing mechanism exactly (so the running filter picks it up with zero new code), keep a
single source of truth, and prefer the precise frames Mark measured over a 20-fps approximation now that we
have them. The `FPS=20` proxy entries are left untouched (reversing them is a separate cleanup, not in scope).

**Trade-off:** Mild inconsistency — 6/15 uses true fps while sibling 2026 entries use the 20-fps proxy. Worth
it: the proxy is an acknowledged approximation, and active-play gating is robust to the ~1% it would cost.

## 2026-06-24: Invalid data-scaling curve is quarantined + regenerated, not trusted

**Context:** The distill data-scaling curve (`G:\ballresearch\distill\curve.jsonl`, N=1..16) was produced
by a stale `iter_run.py` that silently trained on only ~4 games at every N (12/16 games hit a now-deleted
`"NO polygon … skip"` path). The metric was flat — but because of the bug, not because venue diversity
fails to help. A previous session believed the polygon fix had de-risked the curve; the fix was real but
landed *after* the curve ran.

**Decision:** Treat the curve row as untrustworthy on its own. Concretely:
- **Quarantine, never delete:** the bad curve is preserved as `curve.jsonl.buggy4games_<ts>` (history),
  and the curve regenerated from N=1 with the fixed binary.
- **Verify game-count from the run log, not the curve row:** every `iter_N{N}.log` prints
  `train roots (K games)` — `K` must equal the resolved game count, else the point is invalid. The curve
  row's `games` list is the *requested* set, not what trained.
- **One orchestrator owns the curve** (single GPU job; it writes a `curve_gpu.flag` so the variant filler
  yields). Re-running with the fixed code is cheap relative to trusting a silently-broken result.

**Why:** This is the second time a silent data-selection bug (first the fuzzy resolver, now a stale
binary) gutted the curve. The lesson (per "verify before reporting", "verify tracks before videos"):
a green-looking metric file is not evidence the intended data trained — confirm the denominator.

## 2026-06-24: NORMAL-play eval requires a clean on-field + in-band ground truth

**Context:** The curve's NORMAL split scored the detector against AutoCam-detector dets (conf≥0.40)
corroborated only by viewport-x within 500 px — with **no Y constraint and no field-polygon test**.
45% of that "normal" GT lands off-field or outside the eroded eval band-mask (far-sideline corner),
where the detector structurally cannot fire. That caps the NORMAL ceiling well below 1.0 regardless of
model quality, making the "normal-play collapse" partly an eval artifact.

**Decision:** The honest NORMAL metric filters GT to **on-field (inside the raw field polygon, ≤40 px
margin) AND inside the eroded eval band-mask** — i.e. only frames the detector can win. HARD (human far
labels) stays as-is (already 92% in-mask). A standalone `clean_eval.py` (CPU-only, reuses the
`spc_eval_cache` buffers; lives in `G:\ballresearch` scratch, not the repo) reports raw_normal vs
clean_normal side by side so the artifact magnitude stays visible. AutoCam-as-GT in NORMAL remains a
known circularity to audit (human-sample a few normal frames) before any "matches AutoCam" claim.

**Why:** Comparing AutoCam (0.96 normal R15) to a detector scored on targets it's masked out of is not a
fair head-to-head. Measure each side only where the task is winnable, and keep the off-field/masked count
in the report.

## 2026-06-24: NORMAL GT is HUMAN labels, not a filtered AutoCam proxy (supersedes the filter-only fix)

**Context:** The prior entry (same day) proposed making NORMAL honest by *filtering* the AutoCam-derived
GT to on-field + in-band. Investigating further (EXP-DIST-03): filtering still inherits AutoCam's
**circularity** — the proxy only contains balls AutoCam already detected at conf≥0.40, so it can't include
the balls AutoCam misses, which is precisely where the distilled detector is supposed to win. A filtered
proxy raises the ceiling but still can't measure the detector's real edge. Separately, I confirmed (vision
+ y-distribution) that the existing Spencerport human labels (`spc_clip1..5`) are **far-ball** GT — they
correctly feed HARD and are NOT usable as normal-play GT. So no existing human normal-play GT existed.

**Decision:** The honest NORMAL split uses **human normal-play labels**, collected with the same far-label
tool that produced `spc_clip*`. Concretely:
- New set **`spc_normal1`** = Spencerport frames 9460–10020 (141 frames, every-4th) — a continuous
  near/mid-field active-play stretch inside the eval window [7900,11500], in the gap between spc_clip3 and
  spc_clip4. Built by the canonical `G:\ballresearch\farlabel_clip.py`; served by the running annotation
  server from `D:\training_data\far_label\spc_normal1\`.
- **Naming convention:** `spc_normal*` = human NORMAL-play GT; `spc_clip*` / `spc_diverge` /
  `spc_hard_review` = human FAR GT (HARD). The eval split routes by this prefix.
- **Wiring (server scratch `iter_run.py` + `reeval_clean.py`):** HARD ingestion excludes `spc_normal*`;
  NORMAL prefers human `spc_normal*` labels and falls back to the (filtered) AutoCam proxy ONLY while no
  `spc_normal*` labels exist — so the in-flight curve is not disturbed and self-corrects once labeled.
- The filter-only approach from the prior entry is retained solely as the labeled-yet fallback, not the
  target metric.

**Why:** A detector whose whole value proposition is "finds balls AutoCam loses" cannot be honestly graded
against "balls AutoCam found." Human GT is the only non-circular NORMAL reference. The cost is one ~141-
click human pass; the payoff is the first interpretable NORMAL number for the venue-diversity question.

## 2026-06-15: v4 (perspective-normalized, warped) added ALONGSIDE v3 (tile) — additive, not a rename

**Context:** The perspective-normalized full-frame detector was drafted on this branch under the
name "v3", but "v3" was already the tile-based detector lineage (`train_v3.py`, `train.py`'s
`V3_*` config + their tests). Conflating them was confusing.

**Decisions:**
- **The new perspective-normalized, warped-full-frame strategy is designated v4.** The existing
  tile-based detector keeps the v3 name. They are two coexisting strategies.
- **Additive, not a rename.** The v3 path is **fully maintained** and left untouched —
  `train_v3.py`, `training/train.py` (incl. its `V3_*` hyperparameters), the shared `manifest.py`
  dataset knobs, and their tests are unchanged. v4 is added as **new files only**:
  `training/train_v4.py` (warped entry scaffold), `training/data_prep/warped_pack.py`
  (pre-decoded warped-frame shards), `training/experiments/io_benchmark.py` (the I/O gate),
  `tests/test_warped_pack.py`. The perspective **design docs** (PERSPECTIVE_NORMALIZED_DETECTOR.md)
  are relabeled v4 because they describe the v4 strategy — that is correct labeling, not removal of
  any v3 content.
- **train_v4 fixes the v3 starvation:** persistent-worker DataLoader (`workers` default 8, not 0)
  + no train-time JPEG decode (warp once, offline, into shards).

**Trade-off:** Two training entry points (tile v3 + warped v4) and some shared dataset knobs. Worth
it: v3 stays a working fallback while v4 is validated, and the lineage is unambiguous.

---

## 2026-06-15: Reolink capture image profile tuned for on-field color differentiation

**Context:** Far balls wash out against bright sky/sun-flared grass. The fix is capture-side
(can't repair recorded footage). Tuned the "SoccerCam" Duo 3 PoE against a controlled paused scene
and measured each setting objectively on the field region (Lab chroma spread = color separability,
luminance contrast, and saturation/highlight clipping %).

**Findings (measured, not eyeballed):**
- **Color differentiation is near its ceiling at moderate saturation.** `chroma_spread` is ~flat
  (~14) across all settings — more saturation raises vividness (chroma_mean 47→63) but NOT
  separability, and **clips**: saturation 175 → 23% of field pixels saturation-clipped, 185 → 37%.
  Clipping *merges* colors, so over-saturation actively hurts. ⇒ **cap saturation at 150.**
- **WDR (`backLight=DynamicRangeControl`) on** keeps highlight detail in the high-dynamic-range
  sun+field scene; `drc` 150 (110–200 barely moved the metric).
- **`nr3d` (3D noise reduction) is the big un-baked lever:** OFF recovers ~16× more fine detail
  (Laplacian variance 8→160), i.e. it currently smooths away the texture a 3–8px far ball lives in.
  NOT flipped by default — it trades detail for noise that eats the 20 Mbps bitrate. **Open
  experiment:** A/B nr3d on/off (likely with a bitrate bump) on real far-ball footage before
  committing.
- Window/highlight clipping itself is lens/sensor physics and is NOT a goal (per Mark).

**Decision:** Baked the proven profile into `ReolinkCamera.apply_optimal_settings`
(`OUTDOOR_ISP`/`OUTDOOR_IMAGE` in `video_grouper/cameras/reolink.py`): WDR on, drc 150,
dayNight=Color (locked, no daytime IR flip), antiFlicker Off, saturation 150, contrast 140,
bright 118, sharpen 145. `get_current_settings` now reports WDR/sat/contrast/nr3d.

**Caveat:** Tuned on an emissive TV scene; saturation/contrast/WDR are scene-independent processing
curves so this is a sound baseline, but validate on the next real game. `gain` is locked 40–40 and
not settable via the HTTP API on this model, limiting direct exposure control.

---

## 2026-06-15: v4 detector pivots from bbox regression to a heatmap + multi-frame design

**Context:** The first real eval killed the bbox approach. A nano bbox detector trained on the
reference-detector bootstrap at TW=3264, evaluated center-distance vs Mark's human far-ball ground
truth (162 balls): **far-recall 12% @ conf0.05 (precision 22%)**, vs the reference detector's
**74% far-recall / 76% precision**. Two structural causes: (a) at TW=3264 the far ball is ~3.6px —
at/below the detector's smallest stride (8px) and meaningless for IoU-mAP (a 2px miss → IoU≈0);
(b) the bootstrap training labels (from the reference detector) miss far balls, so there's little
far-ball signal to learn. Bbox regression is the wrong tool for a 3–8px ball.

**Decision:** Adopt a **ball-center heatmap + multi-frame (temporal)** detector for v4 with a
lightweight high-resolution-feature backbone; output a per-pixel center heatmap and peak-pick (x,y).
Why it fits us: removes the tiny-box / anchor / stride / IoU failure mode entirely; **exploits motion**
across consecutive frames (our **static camera** makes frame-differencing clean — a lever bbox
regression ignored); center-distance is the native metric (already our eval); **our far-label clicks
ARE heatmap targets** (Gaussian at the click — no bbox relabel); the perspective warp still helps
(scale-normalize + field-band crop → fewer pixels, ball ~uniform size). Detailed survey of candidate
architectures + the no-train baseline comparison are archived on **F:** (external research stays out
of the OSS repo).

**Edge constraint is non-binding:** requirement is 90 min @ 20 fps in <24 h = **~1.25 fps** (≈0.3 fps
at every-4th-frame). Lightweight heatmap trackers run far faster than this on CPU (tens–hundreds of
fps), so realtime is plausible while we optimize for ACCURACY (high input resolution, tiling) not
speed. Hard requirement: stay CPU-executable + export to ONNX/CoreML/TFLite — **no required GPU**.

**Next:** a no-training baseline of a pretrained heatmap model on the Irondequoit clip vs the 74%
reference, then fine-tune on our warped frames + human far-ball labels. The nano bbox run (12%
far-recall) stands as the recorded bbox baseline.

---

## 2026-06-15: `target_width` is a swept speed/accuracy knob (the 1280 warp default is wrong for v4)

**Context:** `field_warp.build_field_warp` resizes the warped band horizontally to `target_width`
(vertical scaled by the same `target_width/src_w` ratio). The module default
`DEFAULT_TARGET_WIDTH=1280` is a 6× horizontal downscale from 7680 → a ~0.08 MP image that crushes a
far ball from ~8.5px to ~1.4px — **below AutoCam's ~3264 working width**, so it cannot beat AutoCam
on far balls. The earlier "~0.08 MP / fits-on-G:" sizing came from this default and was wrong.

**Decisions:**
- **`target_width` is the central speed/accuracy trade-off, not a fixed value.** Ideal is high-res
  (TW≈5120–7680 → ~1.2–2.7 MP warped frames; far field full-res, near field vertically compressed).
  We **sweep** TW ∈ {3264, 5120, 7680} and pick the **lowest** that still **beats AutoCam on
  far-ball recall** at acceptable speed (floor ≈ AutoCam's 3264).
- **Match train + infer resolution.** Training at one TW and inferring at a lower one shrinks balls
  below the learned scale → under-detection (train-test resolution discrepancy / FixRes). Compare
  matched `(train@TW, infer@TW)` pairs; explore downscaling via a short FixRes fine-tune at the
  target TW, not by inferring a high-res model low.
- **Two halves:** the speed axis is measured now (I/O benchmark); the accuracy axis (far-ball recall
  vs AutoCam at each TW) is the downstream resolution experiment that selects the production TW.
- At ~2.7 MP/frame the pre-decoded set is hundreds of GB→>1 TB and does **not** fit on G:, so
  shard-rotation streaming (`warped_pack.ShardRotator`) is required.

---

## 2026-06-12: Field-outline wired into the plugin pipeline as a `field_detect` step

**Context:** The distilled field-outline model (EXP-008) needed to actually drive homegrown ball tracking + rendering. The pipeline's track and render steps already consume a `field_polygon_path` manifest artifact (location filtering, mount-tilt/leveling derivation, off-field rejection, pan bounds) — but nothing produced it automatically.

**Decisions:**
- **New `field_detect` pipeline step** (`video_grouper/pipeline/steps/field_detect.py`), inserted `stitch_correct → field_detect → detect → track → render` in the homegrown preset. Runs the field-outline model on a few sampled frames and keeps the **highest-confidence** polygon (the field is static per fixed camera, so one polygon serves the game). Writes `field_polygon.json` and records `field_polygon_path` in the manifest.
- **The polygon is a required artifact with a neutral default, not an optional one.** With no model configured (or no usable polygon found) the step writes the **full-frame rectangle** (`source: "full_frame"`), and render synthesizes the same default if the manifest has no polygon at all. The full frame is the neutral element of the geometry: centred base pitch, full vertical extent, full pan range, derived mount tilt ≈ 0, and the off-field filter keeps every in-frame detection — one code path whether or not a real field was found.
- **Same two-source loading as the ball model**: `model_key` (TTT-licensed — ships as a **free** TTT-provided model) or `model_path` (local plaintext, dev/community). The shared license-acquisition path moved to `pipeline/steps/licensed_model.py`, used by both `detect` and `field_detect`. No model binary or seeded model identifier in the OSS repo, per policy.
- **No detect-side filtering and no render-side framing logic in this step** — the earlier provider-era draft filtered detections in detect and recentred the crop in render; both are superseded by the pipeline's existing design (track's re-sweepable `track_field_margin` filter, render's polygon-derived geometry). The step only *produces* the polygon.

**Trade-off:** One model load + a few-frame inference per game (cheap, once per game). The full-frame default changes render's no-model behaviour from the hand-tuned config fallbacks (`render_view_pitch_deg`, `render_mount_tilt_deg`, `render_field_half_pitch_deg`) to polygon-derived neutral values — intentional: the polygon is the single source of framing truth.

---

## 2026-06-11: Field-boundary student model via teacher distillation

**Context:** The 10-point field-boundary polygon (used to mask off-field ball detections and to drive the broadcast camera) comes from a third-party "teacher" ONNX model we want to retire. We own the footage, so we can distill it: run the teacher over our Reolink games to auto-label, then train an in-house student. Built as standalone tools (`training/field_outline/` + `training/cli/{generate,train,eval,export}_field_outline.py`), not yet wired into the pipeline task system.

**Decisions:**
- **Direct 10-keypoint regression** — not heatmaps, segmentation, or YOLO-pose. The teacher is itself a direct regressor, so distillation targets map 1:1, and the exported graph stays simple enough to match the teacher's exact I/O.
- **ResNet18 backbone + dual heads** (20-dim coords, 10-dim scores); ImageNet normalization is baked into `forward` so the net takes the same `[0,1]` RGB input `field_detector` feeds the teacher — the exported `.onnx` is a **drop-in** with zero downstream changes. `mobilenet_v3_small` kept as a fallback flag if ResNet18 memorizes.
- **Score head distills the teacher's per-point confidence** (soft-target BCE), preserving the `mean(scores) >= 0.70` gate semantics that downstream code relies on.
- **Store every labelled frame** — never discard low-score/indoor frames at generation (irrecoverable, and they train the score head). The coordinate loss is gated at train time to in-frame, score≥0.5 points of frames the teacher itself trusted.
- **Split by placement (team, venue), never by frame.** The camera is fixed per game, so a frame-level split leaks. Same-named venues across teams stay separate (Flash-"Home" ≠ Heat-"Home"); identical same-team fields merge by median-polygon IoU > 0.85.
- **Augmentation is the primary overfitting defense** (~20 distinct placements): random crop + aspect jitter, horizontal flip with keypoint index remap, photometric, synthetic occlusion.
- **Reolink only** (7680×2160). Dahua 4096×1800 footage is excluded for camera-geometry consistency.

**Trade-off:** The student inherits the teacher's failure modes — it can be no better than the teacher's labels. Indoor/low-score venues train only the score head; human-corrected polygons (the existing `"source":"human"` flow) are the v2 path for those.

---

## 2026-04-15: Single canonical deploy script for remote workers

**Context:** Laptop worker kept dying after reboots and couldn't restart. Root cause: 3 conflicting scheduled tasks (`GPUWorker`, `LaptopWorker`, `PipelineWorker`) each pointing to different hand-edited bat files with wrong credentials, wrong CUDA paths, and TOML backslash escaping bugs. Each time someone fixed a problem they created a new bat/task instead of fixing the canonical deploy script.

**Decision:** `training/worker/deploy_worker.ps1` is the ONE AND ONLY way to deploy remote workers. It handles everything: code sync, config generation (with correct TOML UNC path escaping), startup bat generation, pip dependencies, scheduled task cleanup + registration, and post-deploy verification. Never create bat files, scheduled tasks, or edit configs by hand on remote machines. If a worker stops, re-run the deploy script.

**Trade-off:** Requires PS remoting enabled on remote machines. No support for partial updates — always does a full redeploy. This is intentional: idempotent full deploys are more reliable than incremental patches.

---

## 2026-04-14: Per-game field boundary replaces row-based spatial filter

**Context:** Trajectory building used `row >= 2` exclusion as a crude proxy for "off-field." This missed off-field detections in rows 0-1 and excluded legitimate on-field detections in row 2. Ball trajectory coverage was only 0-5% across all games, partly due to off-field noise polluting trajectory fragments.

**Decision:** Per-game field boundary polygon stored in manifest metadata (`field_boundary` key). Three-tiered detection: ONNX keypoint model (primary, proven on 9 games) → Sonnet vision fallback → human annotation. Trajectory building requires a valid polygon — skips if none available. Uses **soft filtering**: on-field and near-off-field (within 150px) kept, far-off-field excluded. This preserves throw-in/goal-kick continuity per the field-mask-must-be-soft principle.

**Impact:** Row-based `row >= 2` filter removed. Games need a field polygon before trajectory building runs. Human can draw/adjust polygons via annotation app Field tab.

---

## 2026-04-07: SQLite manifest + pack files replace loose tile/label files

**Context:** 7.7M loose JPEG tiles across 39 games on HDD. `os.listdir` on 300K-file directories takes 5+ minutes. Label files (500K .txt) are equally slow to scan. Everything is I/O-bound on HDD random reads.
**Decision:** Single `manifest.db` (SQLite WAL mode) as the source of truth for all tiles and labels.
- Schema: `games → segments → frames → tiles → labels` hierarchy
- Tile inventory: every .jpg cataloged with game_id, segment, frame_idx, row, col
- Pack files: all tiles for a segment concatenated into one `.pack` binary file, manifest stores (pack_file, pack_offset, pack_size) per tile
- Labels: YOLO bounding boxes stored in labels table (replaces 500K .txt files)
- Verification: `verify_tiles.py` queries manifest instead of scanning filesystem (~2ms vs ~5 min/game)
- Benchmark: pack file reads 245x faster than loose file reads on HDD (21K tiles/sec vs 29/sec cold)
**Trade-off:** One-time migration cost (~5hrs catalog + ~20hrs pack). DB is ~2GB. Pack files are same total size as loose files but 6 files per game instead of 300K.
**Files:** `training/data_prep/manifest.py`, `training/data_prep/verify_tiles.py`

## 2026-04-07: ONNX labeling writes to local manifest.db, merged on server

**Context:** Remote machines (FORTNITE-OP, laptop) run ONNX detection but can't directly write to the server's manifest.db.
**Decision:** `label_job.py` writes detections to a local `manifest.db` on the remote machine. After labeling completes, transfer the DB to the server and merge via `manifest merge`. Auto-backup before merge.
**Trade-off:** Extra transfer + merge step, but keeps the remote script self-contained (no network DB dependency during inference).
**Files:** `training/distributed/label_job.py`, `training/data_prep/manifest.py` (backup_db, merge_labels_from)

---

## 2026-03-31: Distributed tiling — laptop CPU helps while GPU trains

**Context:** 26 games need tiling, server takes ~30 min/game = ~13 hours alone. Laptop GPU is busy training but CPU is idle.
**Decision:** mass_tile.py supports `--remote` mode. Laptop reads video from network share, tiles locally, writes tiles to server's D: via share. Lock files (`.locks/{game_id}.lock`) prevent both machines from tiling the same game. 2-hour stale lock timeout.
**Trade-off:** Network I/O (~100 MB/s gigabit) is slower than local D: for video reads, but it's free CPU cycles. H.264 decode is CPU-bound anyway.
**Commands:**
- Server: `uv run python -m training.data_prep.mass_tile`
- Laptop: `uv run python -m training.data_prep.mass_tile --remote \\192.168.86.152\video \\192.168.86.152\training`

## 2026-03-31: Use YOLO26 for v3 training (upgrade from YOLO11)

**Context:** YOLO26 (Jan 2026) adds Small-Target-Aware Label Assignment (STAL) and Progressive Loss (ProgLoss) — built-in improvements for small object detection. Our ball is 8-40px, exactly the target scenario. SoccerDETR paper showed +3.2% ball mAP from scale-aware loss.
**Decision:** Switch from `yolo11n.pt` to `yolo26n.pt` for v3. Already available in our ultralytics 8.4.27 install — no package changes needed.
**Alternatives:** Custom Scale-Aware Focal Loss on YOLO11 — rejected because YOLO26 provides this natively.

## 2026-03-31: v3 is a continuous improvement loop, not a one-shot build

**Context:** Previous versions (v1, v2) tried to get labels right THEN train. This led to weeks of label work before any training started.
**Decision:** v3 starts training with imperfect labels from all 35 games. Human-in-the-loop and Sonnet fill gaps as training runs. Each model iteration finds more balls, reducing gaps. The loop converges naturally.
**Reason:** 4x more data with okay labels beats 1x data with perfect labels. The continuous loop means label quality improves alongside model quality.

## 2026-03-31: Game IDs include timestamp suffix for same-date games

**Context:** Two Flash games on 2025.05.04 produced duplicate game_ids (`flash__2025.05.04`).
**Decision:** Append HHMMSS from folder name when a time component exists (e.g., `flash__2025.05.04_031801`).
**Alternatives:** Sequential suffix (_a, _b) — rejected because timestamp is self-documenting and stable.
**Impact:** Renamed `flash__2025.06.02` to `flash__2025.06.02_181603`. Updated OLD_TO_NEW mapping.

## 2026-03-31: Frame extraction every 4th frame (not 8th)

**Context:** Previous `process_batch.py` used `FRAME_INTERVAL = 8` (~3 fps from 24.6 fps source).
**Decision:** Use `FRAME_INTERVAL = 4` (~6 fps) for denser coverage and more continuous ball tracking.
**Impact:** 2x more tiles per game, 2x more training data, ~2x longer tiling time.

## 2026-03-31: Recursive segment search for tournament games

**Context:** Tournament game folders have sub-folders (Game 1/, Game 2/, Game 3/) with [F] segments inside. Registry scanner only searched top level, missing all tournament games.
**Decision:** Changed `gdir.iterdir()` to `gdir.rglob("*.mp4")` in `build_registry()`.
**Impact:** Found 3 new games: Hershey Tournament (17 segs), and correctly detected Heat Tournament + Clarence Tournament.

## 2026-04-13: Upside-down game handling — `needs_flip` flag

**Context:** 9 games (May–June 2024) were recorded with the Dahua camera mounted upside down. Some have corrected `-raw.mp4` files but we don't use them — we always process the individual `[F]` segment files and flip in code.

**Decision:** The game registry has a `needs_flip INTEGER DEFAULT 0` column. When `needs_flip=1`:

1. **Tiling** (`tile.py:288`): `cv2.flip(frame, -1)` before cutting tiles → tiles are right-side up
2. **Labeling** (`label.py:161`): `cv2.flip(frame, -1)` before ONNX inference → detections are in right-side-up coordinates matching the tiles
3. **Prescan** (`label.py:268`): also flips before sampling frames for game detection

The flip is carried via the task **payload** (`{"needs_flip": true}`), built by:
- Orchestrator `_build_payload()` for both `tile` and `label` tasks (reads from game registry)
- CLI `cmd_enqueue()` also reads from registry when manually enqueueing

**Critical:** If a task is enqueued WITHOUT a payload (e.g., old queue items, direct DB insertion), `needs_flip` defaults to `False` and flipped games will be processed upside down. Always enqueue through the orchestrator or CLI.

**Flipped games (as of 2026-04-13):**
- `flash__2024.05.01_vs_RNYFC_away`
- `flash__2024.05.10_vs_NY_Rush_away`
- `flash__2024.06.01_vs_IYSA_home`
- `flash__2024.06.02_vs_Flash_2014s_scrimmage`
- `heat__2024.05.13_vs_Byron_Bergen_home`
- `heat__2024.05.19_vs_Byron_Bergen_home`
- `heat__2024.05.28_vs_Chili_home`
- `heat__2024.05.31_vs_Fairport_home`
- `heat__2024.06.04_vs_Spencerport_home`

**Files:** `training/pipeline/registry.py` (schema), `training/pipeline/orchestrator.py` (`_build_payload`), `training/tasks/tile.py` (flip at line 288), `training/tasks/label.py` (flip at lines 161, 268), `training/pipeline/__main__.py` (`cmd_enqueue` payload)

## 2026-03-30: Game naming convention

**Context:** Games had inconsistent IDs (old: `flash__06.01.2024_vs_IYSA_home`, tournament: `heat__Heat_Tournament`).
**Decision:** Standardized format: `{team}__{YYYY.MM.DD}_vs_{opponent}_{location}`. Teams lowercase, double underscore separator, date in sortable YYYY.MM.DD, single underscore between words, no spaces/parens.
**Impact:** Renamed all existing tile/label directories. Added OLD_TO_NEW mapping in game_registry.py.

## 2026-03-29: 3-class detection (game_ball / static_ball / not_ball)

**Context:** Binary ball/no-ball lost the distinction between the active game ball and static balls on sidelines (cones, spare balls, equipment).
**Decision:** Trajectory analysis classifies detections. Moving trajectory (path_length > 50px or max_speed > 20px/frame) = game_ball. Trajectory ≥3 frames but barely moves = static_ball. Isolated 1-2 frame detection = not_ball. QA verdicts override trajectory classification.
**Result:** 272K game_ball, 116K static_ball, 93K not_ball labels.
**Alternatives:** Binary detection + field mask filtering — rejected because field masks are imprecise and we lose sideline context.

## 2026-03-28: Independent workers replace Dask coordinator

**Context:** Dask dashboard websocket flooding DOSed the scheduler event loop. Workers disconnected every time coordinator restarted. Single point of failure.
**Decision:** Filesystem-based job queue (`jobs.py`) with independent workers. Jobs are JSON files in pending/active/done/failed directories. Atomic claim via `os.rename`. No coordinator process needed.
**Alternatives:** Ray — doesn't support Windows. Celery — too heavy for 3 machines. Dask — tried, failed due to dashboard bug and SPOF design.

## 2026-03-27: Tar shards for dataset portability

**Context:** Copying 275K individual tile+label files over network took hours and had high failure rate. SMB random I/O on USB drive was 5 MB/s.
**Decision:** Package dataset into ~200 MB tar shards organized by split/game/zone. Sequential reads, one file copy per shard, extract locally before training.
**Alternatives:** SQLite database per game — considered but YOLO expects filesystem layout. WebDataset streaming — considered for future.

## 2026-03-26: Relay training (server always trains, helpers preempt)

**Context:** 3 machines available but kids use 2 of them for gaming. Need server to always be productive, helpers to train when idle.
**Decision:** Server trains continuously with `train_relay.py`. When a faster GPU (laptop RTX 4070 or Fortnite-OP RTX 3060 Ti) becomes available, it preempts server training after current epoch via lock file + heartbeat.
**Alternatives:** Round-robin scheduling — rejected because server GPU should never be idle.

## 2026-03-22: Dataset uses folder structure, not train.txt file lists

**Context:** YOLO supports both `train.txt` (file lists) and folder-based dataset layout.
**Decision:** Use `images/{train,val}/{game}/` with NTFS hardlinks to tiles that have matching labels. YAML config uses folder paths.
**Reason:** Folder structure is simpler to maintain, YOLO creates .cache on first scan, no file list management.
**Impact:** Train: 348K tiles (13 games), Val: 46K tiles (2 games).

## 2026-03-22: Exclude upside-down games from v2 training

**Context:** `flash__2024.06.01_vs_IYSA_home` and `heat__2024.05.31_vs_Fairport_home` recorded with camera mounted upside down (sky at bottom, spectators at top).
**Decision:** Exclude both from v2 training dataset. For v3, include them with corrected video or code-flipped tiles.
**Reason:** Including upside-down frames would confuse the model about field orientation.

---

## 2026-04-09: HTTP API-only architecture for pipeline

**Context:** Multiple machines (server, laptop, FORTNITE-OP) need to coordinate work. Direct SQLite access from remote machines causes corruption.
**Decision:** Only the PipelineAPI process touches SQLite (registry.db, work_queue.db). Workers communicate exclusively via HTTP API. SMB shares are for bulk file transfer only (packs, manifests, videos).
**Trade-off:** Extra HTTP round-trips, but eliminates all SQLite concurrency issues.
**Files:** `training/pipeline/api.py`, `training/pipeline/client.py`, `training/worker/worker.py`

## 2026-04-09: Sonnet QA with Claude CLI for ball detection verification

**Context:** ONNX model produces many false positives (~85% NOT_BALL). Need automated QA before training.
**Decision:** Use `claude -p` CLI with Sonnet model to verify detections. Build 3x2 grid composites of tiles, ask Sonnet BALL/NOT_BALL for each. ~10s per grid, 120 tiles per game, included in Claude Max subscription.
**Trade-off:** Slower than a dedicated classifier, but zero additional cost and high accuracy.
**Files:** `training/tasks/sonnet_qa.py`

## 2026-04-10: Per-segment tiling — skip concatenated videos

**Context:** Video directories contain both individual segment files (`18.30.10-18.46.58[F][0@0][215198]_ch1.mp4`) and concatenated full-game videos (`flash-iysa-home-raw.mp4`, `combined.mp4`). Concatenated videos produce 100GB+ packs that fill the SSD.
**Decision:** Only tile files with `[F]` or `[0@0]` markers in the filename. Skip all others. Individual segments cover the same footage in manageable ~15-20GB chunks.
**Files:** `training/tasks/tile.py` (skip filter at line ~60)

## 2026-04-10: Legacy label remapping — raw frame indices to per-segment

**Context:** Legacy labels reference concatenated "raw.mp4" frame numbering (e.g., frame_idx=98444). New tiles use per-segment numbering (e.g., frame_idx=22694 within segment 4). Frame indices map cleanly via cumulative offset table.
**Decision:** `remap_legacy_labels()` builds offset table from individual segment .mp4 files, remaps label tile_stems and tile frame indices in the manifest. Both tiles and labels end up using per-segment frame numbering.
**Files:** `training/data_prep/game_manifest.py` (`remap_legacy_labels()`)

## 2026-04-11: F: as permanent pack archive, D: as serving tier

**Context:** D: (HDD, 1.9TB) can't hold all pack files (~80GB/game × 30+ games = 2.4TB). F: (USB, 15TB) has ample space.
**Decision:** Pack lifecycle: create on G: SSD → push to D: (for SMB serving) → archive to F: (permanent) → clean D:. When a task needs packs, `server_packs()` auto-restores from F: to D:. Remote workers access D: via SMB; they never see F:.
**Manifest convention:** `pack_file` column always stores D: paths. The system transparently stages from F: when D: copies don't exist.
**Trade-off:** Extra copy F:→D: when accessing old games. But D: stays small and we never run out of space.
**Files:** `training/tasks/io.py` (`server_packs`, `cleanup_server_packs`), `training/tasks/tile.py` (archive step), `training/data_prep/manifest_dataset.py` (`_resolve_pack_path`)

## 2026-04-11: Python 3.13 standardized across all machines

**Context:** Server had 3.13, laptop had 3.12. CUDA DLLs were available in system Python's PyTorch installation but not in the uv venv's PATH.
**Decision:** All machines use Python 3.13. Worker startup bat files add `C:\Python313\Lib\site-packages\torch\lib` to PATH for CUDA 12 DLLs (cublas, cudnn, cufft, etc.). No separate CUDA toolkit installation needed — PyTorch bundles everything.
**Files:** `training/pipeline/run_laptop_worker.bat`, worker pyproject.toml (`requires-python = ">=3.13"`)

## 2026-04-11: Flywheel improves training data, not labeling model

**Context:** The pipeline uses an external ONNX model for initial ball detection labels. Our trained model may or may not be better.
**Decision:** The flywheel cycle improves the training DATASET, not the labeling model:
1. External ONNX labels (`source='onnx'`) — baseline, always preserved
2. Sonnet QA verdicts (`qa_verdict`) — automated verification, accumulates
3. Human reviews (`source='human_gap_review'`) — highest-value labels from trajectory gaps
4. Training uses all verified data to build our model
5. Our model is only deployed for labeling if it demonstrably outperforms the external model on the human-verified test set

Label sources are tracked separately so we can always compare model performance against ground truth. We never overwrite external model labels — QA verdicts and human labels are additive.
**Files:** `training/data_prep/game_manifest.py` (labels table: source, qa_verdict columns)

## 2026-04-11: Ball track length is the ground truth metric

**Context:** Multiple metrics could indicate model quality — precision, recall, mAP, false positive rate. But the purpose of the model is to track the game ball continuously.
**Decision:** The primary metric is **verified game ball track length** — the longest continuous trajectory confirmed by the human reviewer, measured as a percentage of total game time. This directly measures what we care about: can the model see the ball throughout the game?
- Sonnet QA helps filter false positives but isn't perfect
- Only human verification of the trajectory confirms ground truth
- Retraining is valuable when track gaps exist that new labels could fill
- The flywheel naturally converges: longer tracks → fewer gaps → fewer human reviews → less retraining needed → done
**Trade-off:** Harder to measure automatically than mAP. Requires trajectory building + human review to evaluate. But it's the metric that actually matters for the autocam use case.
