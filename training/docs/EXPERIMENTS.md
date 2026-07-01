# Experiment Log

Each experiment has: hypothesis, method, result, conclusion. Failures are as valuable as successes.

---

## EXP-PHASE-14: untrimmed-KO — opt-in localize + full-field BAND anchor (resolves 13) (2026-06-30)

**Goal:** push combined-video KO past ff04e6d's whistle-clear anchor without the trust-precision
collapse EXP-PHASE-13 hit when it tried the full-field anchor. Measured on the now-**16-game**
combined cache (`phase_cache_comb\`, `_combval.py --localize`), two-front gate = trimmed stays reolink
53/63 AND combined KO improves with few confident-wrong trims.

**Two changes vs ff04e6d (both load-bearing):**
1. **Opt-in `localize`** — `fuse_phases(signals, *, localize=False)`; the trimmed CLI + fixtures fuse
   with the default (never call `locate_game_block`), `detect_phases` (production, combined) passes
   `localize=True`. This makes the 53/63 guarantee **structural** (localization CANNOT touch trimmed),
   so `locate_game_block` is now free to be aggressive without any trimmed risk. Verified: trimmed
   reolink **53/63, END 12/12 byte-identical**; 3 fixtures pass; ruff clean.
2. **Full-field BAND anchor** — KO whistle = first blast with in-field count in
   `[0.55, 1.8] × busy` (busy = p75 of the smoothed curve) AND not-cleared. EXP-PHASE-13's full-field
   anchor used only a LOWER bound and over-localized onto the **dense both-teams-warming-up crowd**
   (06.06 warm-up ~3.0× busy read as "full" → +323s, ok=True). **The UPPER bound (1.8) is the fix:** it
   rejects that crowd (06.06-Lakefront-Sullivan warm-up 2.2-3.0× excluded, real KO 1.33× kept → -1s).

**Result (combined, 14 scoreable non-truncated reolink):** KO≤10s **9/14** (ff04e6d baseline 8/13);
**05.31-Spencerport -83s→-1s** (warm-up whistles at 0.44× busy dropped, kickoff at 0.56× kept — this
removed a ff04e6d DANGEROUS). Trust: **7/9 trusted-KOs correct**; the 2 wrong-trusted shrank from
ff04e6d's -83/-154s to **-40s (06.04) / -42s (06.10)** — small enough that the S3 NTFY screenshot
verify-loop catches them.

**Failed levers this session (reverted / not baked):**
- *Curve block_start = onset of longest sustained high-play run:* the steadiest high-play is MID-half,
  so block_start landed at 26:02 / 37:02 etc., overshooting KO. Scored 3-4/13. Confirms EXP-PHASE-13's
  "curve head too noisy" for onset localization.
- *Bench-dip via loosened clear (`LONG_CLEAR 120→72s`, `WINDOW 75→180s`):* fixed 05.30-Western-NY
  (bench dip 2:50-4:14 skips its 0:18 warm-up whistle → 4:55 -2s) but **broke 05.10** (+258s DANGEROUS
  — its first-half dip 12:26-14:02 empties for 108s and now reads as a lineup clear). **Bench dips and
  first-half stoppage dips are indistinguishable by duration/depth/timing at 12s sampling** — loosening
  to catch one catches the other. Kept the safe 120/75.
- *Center-ball-restart refinement of the anchor:* the sidecar's center restarts are scattered mid-play
  center passes, NOT the kickoff (06.04 has zero within 75s of its real KO). Not usable as a KO signal.
- *Symmetric-prior cross-check for trust (untrust when anchor disagrees with HT-(END-2H)):* flags the
  wrong 06.04 (anchor 7:57 vs sym 8:43) but ALSO the correct 05.31-Spencerport (its 2H is -104s, so sym
  is 104s off) — no clean separation. Not baked.

**Honest bottom line:** combined KO **9/14 (64%)** vs trimmed **53/63 (84%)** — a real +1 over ff04e6d
with smaller dangerous errors and a cleaner (structural) safety guarantee, but still short of the
trimmed bar. The 5 misses are each genuinely hard from audio + player-curve alone: no-whistle video
(03.21 — correctly untrusted), warm-up whistle over a populated field with no bench dip (05.30-Fair
— untrusted), bench dip indistinguishable from a first-half dip (05.30-West — untrusted), **missing
KO whistle** (06.04, real KO 8:37 silent → anchors the 7:57 pre-kick whistle, -40s), KO field-count
below the band (06.10, 0.50× busy, -42s). 3 of 5 are correctly UNTRUSTED (→ NTFY, no regression); 2
are trusted but only -40s and caught by human verify. This is the robust ceiling for this signal set;
a materially higher combined KO needs a signal the 180° audio+curve doesn't carry (e.g. the homegrown
ball detector's kickoff-restart once it exists, or a finer head curve that still can't separate the
two dip types here).

---

## EXP-PHASE-13: untrimmed-KO fix — locate_game_block + KO anchor (partial) (2026-06-18)

**Goal:** get KO reliable on the untrimmed combined video (the production regime) toward the trimmed
53/63 bar. Iterated against CACHED combined signals (`G:\ballresearch\phase_cache_comb\`); two-front
gate = trimmed scorecard stays reolink 53/63 (END 12/12) AND combined KO improves.
**What works (committed b7af1a1 -> 1d3bc1d -> ff04e6d):**
- `locate_game_block` wraps the validated fusion (renamed `_fuse_core`): on a combined video it anchors
  to the KICKOFF WHISTLE (first blast not followed by a sustained field-clear -- warm-up whistles
  precede the teams lining up), slices signals to the game block, fuses, offsets back. Trimmed uploads
  return `(0,dur)` = byte-identical no-op (53/63 + fixtures unaffected -- verified).
- KO = the kickoff-whistle anchor on a localized video (first blast >= block_start), not the fuse's
  within-block re-derivation (05.27 KO +54 -> -1s).
- `ko_trustworthy` = whistle/kick-anchored AND not-far-from-onset AND (ok OR localized) -- so a youth
  game with an exact KO but failed HT/2H (ok=False) is still trusted if localized; wrong-KO games stay
  untrusted (fall back to NTFY, never a confident mis-trim).
**Result (combined scorecard, 8 games cached so far):** KO<=10s **4/8** (05.10 -2, 05.07 -1, 05.27 -1,
05.28 -3); trustworthy **3/8, precision 3/3** (never trusts a wrong KO). Trimmed **53/63 unchanged**.
**Failed approach (reverted):** replacing the whistle anchor with a PLAYER-CURVE "first sustained
full-field run" onset (Mark's bench-dip idea) -- the head of the curve is too noisy, so it localized
TRIMMED videos and picked wrong onsets on combined: trimmed 53->44/63, combined KO 4->1/8. The
whistle-clear anchor is more reliable than the player-curve onset. Reverted cleanly.
**Honest state:** SAFE (no confident-wrong KO; wrong ones -> NTFY) with zero trimmed regression, but
NOT yet at the trimmed bar on accuracy. Hard remaining cases: no-whistle combined video (03.21,
`n_blasts=0`, flat curve -- likely un-doable, NTFY), recording-starts-at-kickoff (05.09 GT KO=0:00,
first whistle is a foul -- edge case even trimmed), warm-up whistle mistaken for KO where no clear
follows it (05.30 -261s, the whistle-clear heuristic's weak spot). Full 18-game combined scorecard
pending the signal cache (`_cacheall` on the box, ~10 games left).

**More attempts (Mark's hints: "one clear anchor per game, work forward/back" + "combine factors:
whistle + players + static ball + timing") -- net-negative, reverted to ff04e6d:**
- *Opt-in `localize` flag* (fuse_phases localize=False default; detect_phases -> True): CLEAN idea --
  makes the trimmed 53/63 guarantee STRUCTURAL (localization never runs on trimmed) instead of relying
  on a no-op. Worth keeping if this line is resumed.
- *Combination anchor: KO whistle = first whistle over a FULL field* (both teams on; warm-up whistles
  are over a sparse field). Verified the discriminator on the signals: 05.30 kickoff 6:04 @ 11 players
  vs warm-up 1:45/2:03/2:55 @ 4-6; 05.27 9:25 @ 7 + ball-restart vs warm-up 4:50 @ 2. It **fixed 05.30
  combined KO -261 -> -2s** (real win) and lifted combined KO 4/8 -> 6/13. BUT it **OVER-localizes**:
  it localized ~7/13 games, ~5 to a WRONG block, and those pass the sanity gate self-consistently
  (06.06 +323, 05.31 +72, both ok=True), so trust precision collapsed (whistle-clear: 3/3 -> full-
  field: 2/7). No trust rule (require ok / require localized / not-far) recovered precision because the
  wrong localizations look valid. The accuracy gain is not worth the safety loss.
**Conclusion:** the whistle-clear localization (ff04e6d) is the precise sweet spot -- conservative
(localizes few games, but correctly -> precise trust), which is what a safe auto-trim needs. Reliably
LOCALIZING the game block on an untrimmed combined video is the hard open problem; player-count-based
anchors (onset, full-field) improve accuracy but over-localize. **Honest bottom line: combined KO is
accurate for ~half the whistle-capable games and SAFE (wrong KO -> NTFY) with zero trimmed regression,
but NOT at the trimmed 53/63 bar.** Next idea if resumed: a per-game CONFIDENCE score for the block
localization (only trust high-confidence blocks) rather than a binary localized flag; or anchor on the
HALFTIME multi-whistle (loud, mid-game) where present and derive KO from the equal-halves structure.

---

## EXP-PHASE-12: untrimmed (combined-video) KO fails — diagnosis (2026-06-18)

**Why:** productionizing phase detection to (maybe) replace the NTFY game-start question needs the
detector to run on the **untrimmed combined video** (warm-up -> game -> post-game), not the trimmed
game-only upload it was tuned on. Validation via `detect_phases` on combined videos (real polygon)
vs human GT: 05.10 KO -2s / 05.07 -1s (clean), but 03.21 **KO +527s with ok=True**, 05.09/05.27/05.28
hundreds of s off. So it does NOT reliably find the game start untrimmed.
**Method:** cached combined-video signals (`G:\ballresearch\phase_cache_comb\<gid>.json`) + dumped
`fuse_phases(debug=True)` internals for 03.21 (confident-wrong late), 05.27 (early, ok=False), 05.10
(clean) — so fix iteration is now instant.
**Three mechanisms:**
1. **No-whistle combined video (03.21):** `n_blasts=0` (the combined.mp4 audio lacks the whistle even
   though the trimmed upload has it) + very low counts (`med_play=4`, so the 0.6x full-field gate is
   meaningless). KO falls to the symmetric prior; HT is also misplaced; the result is internally
   symmetric (h1≈h2≈31.6) so the **sanity gate passes (ok=True)** -> a confidently-wrong shifted fit.
   This is the dangerous case (would trim 9 min into the game).
2. **Warm-up / mid-half artifacts (05.27):** first whistle 4:50 is warm-up, first multi 25:37 is a
   first-half foul; the "first whistle / first multi" heuristics grab them instead of the real HT
   (49:28), shifting everything early. Sanity gate caught it (ok=False -> NTFY). Safe but wrong.
3. **Clean structure works (05.10):** one unambiguous HT dip (field empty 51:00-57:00) + KO whistle
   10:04 -> all four within 3s. Proof the fusion logic is fine WHEN the game block is located.
**Root cause:** the fusion assumes the game fills the video (true trimmed, false untrimmed) via
"first X" heuristics; it never LOCATES the game block. The robust anchor is the **one halftime dip
with sustained play on BOTH sides** (warm-up has no such dip; post-game empties but never refills).
**Fix plan (next):** (a) locate the game block = longest sustained-activity region bracketing the one
central full-both-sides HT dip; run the (validated) fusion anchored to that block, ignoring warm-up
/post-game tails. (b) anchor KO/HT to whistles/multis WITHIN the block, not the first in the video.
(c) SAFETY: a KO not whistle/kick-anchored (symmetric-only) or a no-whistle combined video is NOT
trustworthy for trimming -> force NTFY fallback; and reject a KO far from the block's activity onset
(kills 03.21's ok=True). (d) **Two-front validation per change: must not regress trimmed 53/63** while
improving combined-video KO. Approach cleanly as a `locate_game_block` pre-step so the validated
`fuse_phases` core stays intact.

---

## EXP-PHASE-11: wind whistle FPs — no global gate possible; trailing-gust END guard (2026-06-30)

**Goal (Mark): handle wind globally, not END-only — "wind is usually all game."** Correct: the global
loudness gate (below) fixed not just 06.06-S END but also West Seneca HT (+163->-2) and 05.30 HT
(+18->0), both wind FPs at other boundaries. Wind FPs are an all-game phenomenon.
**Infrastructure:** `whistle_blasts` now also returns a per-blast peak-loudness ratio (max frame
loudness / p75); cached as `blast_loud` and populated for all GT games via a new `--recompute-whistle`
(re-runs only the audio whistle pass, keeps the slow cached player-curve + ball signals).
**Global gate -- FAILED.** Swept a global "drop blasts < Nx p75" gate: at 12x, reolink within-10s
**52 -> 44** -- it fixed 3 (06.06 END, West Seneca HT, 05.30 HT) but BROKE 7 (05.07 HT +449, 05.27
KO-275/END-747, 05.31, 06.01, 06.07, 06.10) by dropping their quieter REAL whistles. Tested
pitch-stability (loudness-independent) as the alternative axis -- ALSO fails: in poor-audio games the
real whistle is itself faint AND pitch-smeared (05.07 HT 6.8x / 394Hz spread; West Seneca END 5.5x /
289Hz; vs 06.06 clean whistles 17-25x / 11-31Hz). A wind-masked / distant-ref whistle is
indistinguishable from a wind gust by loudness OR pitch -- the separating information is not in the
audio, so ANY global gate necessarily over-prunes those games.
**Fix that works -- trailing-gust END guard.** Wind resonating at the ref pitch fires a FAINT "multi"
in the minute AFTER the real full-time whistle (06.06-S: real 80:14 ~30x, gust multis 80:51/81:03
~8-11x; the "last late multi" rule grabbed 81:03 -> +48s). Override the last late multi ONLY when it
is faint (< LOUD_GATE x p75) AND a loud multi sits within 120s before it -- that loud multi is
full-time, the faint trailer is wind. A loud multi is NOT preferred over a quiet final whistle in
general (that regressed an END to +746s by picking a loud mid-half foul), so quiet-final-whistle games
are untouched.
**Result (reolink, vs human GT):** 06.06-S END +48 -> **-1s**; reolink END **11 -> 12/12** (every
scoreable END within 3.1s), within-10s **52 -> 53/63 (84%)**, median 1.1s, KO/HT/2H unchanged, zero
regressions. LOUD_GATE default 12x (env `PHASE_LOUD_GATE`).
**Open:** HT wind FPs (West Seneca +163, 05.30 +18) are a different mechanism (not "trailing" a louder
whistle) and a loudness gate there would drop 05.07's faint 6.8x real HT -- deferred; needs a
non-loudness signal (e.g. corroborate HT against the player dip, which it already partly does).

---

## EXP-PHASE-10: remaining two full-file misses are within-tolerance edge cases (2026-06-30)

After EXP-PHASE-07/08/09 the only full-file (non-truncated) reolink misses left are both small and
both within Mark's 3-min bound; chased both, neither warrants a risky change now.

**06.06-S END +48s (whistle false positives).** GT END 80:15 is a real, loud, pitch-stable whistle
(verified by re-decoding the audio: f0 locked at 3575-3618 Hz, loudness up to ~30-37x the 75th-pct,
tonality 0.78-0.84). Mark confirmed by ear there is NO whistle at the two later detected "multis"
(80:46, 81:03). Re-decode shows those are rising/falling-pitch CROWD/VOICE sounds (80:46 glides
2842->3230 Hz, 80:55 sweeps up then down) at only ~4-12x p75 -- the STFT's +/-300 Hz pitch band
catches part of the glide, so `whistle_blasts` fires a false multi, and the END "last late multi"
rule latches onto post-game crowd noise. **Clean discriminators: pitch STABILITY (a ref whistle
holds one pitch; a cheer glides) and LOUDNESS (real is ~3-8x louder).** Not fixed: it needs a
pitch-stability/loudness filter inside `whistle_blasts`, which changes global blast detection and
requires re-decoding every game's audio + re-verifying the 52/63 -- real regression risk for a miss
already inside tolerance. Logged as the next whistle-precision improvement.

*Tried (Mark's suggestion): wind-noise reduction as a pre-filter (PR #77 `experiment/wind-noise-
reduction`).* Re-decoded 06.06-S at 16 kHz and ran the whistle STFT on the original vs `afftdn=nf=-25`,
`hp120+afftdn`, `afftdn=nr=25:nf=-30:tn=1` (tracked), and `hp120+afftdn_tracked`. **Negative result:**
every afftdn variant keeps the real 80:15 whistle BUT leaves the 80:51/80:55/81:03 gust FPs intact
(identical end-window blasts), and the tracked variant ADDS blasts (23->31 multis). afftdn removes a
broadband noise FLOOR; these FPs are a TONAL transient (a gust resonating through the mic at ~3.4 kHz),
which spectral denoise is designed to preserve -- consistent with WIND_NOISE.md (afftdn shaves only
~0.7-4.6 dB off the gust; the `noisereduce` hybrid, not on the box, is "modest" too). Conclusion: wind
denoise is the wrong tool for tonal-gust whistle FPs; the discriminator must be pitch-stability +
loudness inside `whistle_blasts`, not audio pre-cleaning.

*Follow-up experiment (06.06-S only, per Mark): a per-blast LOUDNESS gate.* Measured per detected
blast its peak loudness (xp75 frame loudness) and pitch spread. The real END whistle (80:14/80:15)
is 16-21x p75; the three gust FPs (80:51/80:55/81:03) are only 5.5-9.7x. A peak-loudness gate of
**>=10-14x p75** (comfortable margin) kills all three FPs and resolves END to 80:14 (= GT 80:15, +48s
-> ~+1s) while KEEPING every real boundary (KO 17.6/14.3x, HT 20.9/24.8x, 2H 17.4x). Loudness alone
sufficed -- pitch-stability not even needed. A referee whistle is genuinely loud + pitch-locked; the
gust whistle-through-the-mic is quieter + sweeps. **Caveat before rollout:** a global loudness gate
could drop a quieter wind-masked real whistle in another game, so a full rollout needs --recompute on
all games + full scorecard re-verify (at native 44.1kHz, re-calibrating the multiple). On 06.06-S the
fix is clean and unambiguous.

**06.10 KO +24s (upload trimmed exactly to the kickoff).** Mark confirmed the YouTube/trimmed upload
starts right at the kick (field full of players from frame 0; GT KO = 0:00 trimmed). There is no
pre-kick signature (no static-center-ball restart, no kickoff whistle in-frame) so the detector
latches onto the first in-play foul whistle at 0:24. A guard (`ko_sym<=15` + field-full-at-frame-0
-> KO=0:00) both missed 06.10 (its ko_sym is ~18s) and regressed another game's KO (13->12/15), so
it was reverted. Left as a within-tolerance limitation; not truncation (the kickoff IS captured, at
frame 0) so it is not flagged.

**State:** reolink within-10s **52/63 (83%)**, median **1.1s**; KO 13/15, HT 14/18, 2H 14/18, END
11/12. All large full-file misses fixed (EXP-PHASE-07/08/09). The remaining HT/2H reolink misses are
all in truncated games (06.07 BU15/Lakefront, West Seneca, 03.21) or dahua (no whistle).

---

## EXP-PHASE-09: 2H anchors to the FULL-field refill whistle (closes 05.28) (2026-06-30)

**Hypothesis (verified on cached signals):** 05.28 2H -92s. The 2H whistle picker took the first
whistle after the HT dip offset on2, which is a stray warm-up whistle blown while players are still
trickling back (48:06, only 5 in-field). The REAL 2H whistle is a detected blast at 49:38 (= human
GT) where the field is FULL (11 in-field = med_play); the ball model missed the 2H restart, and the
only nearby restart is a no-whistle one 86s later (51:04, a mid-half re-acquire).
**Method:** (1) `w2_full` = first whistle after on2 where the field is full (`pcount_at >= 0.6x
med_play`); use it as the refill whistle, falling back to the first whistle after on2 when none is
full. (2) when the refill whistle is full-field-corroborated, narrow the "trust a later no-whistle
ball restart over the whistle" window from 90s to **60s** -- the kickoff ball moves within ~a minute,
so a restart 86s later (05.28 51:04) is mid-half (keep the whistle), but a restart ~25s after a
pre-kickoff whistle (06.10 50:48 after 50:23) is still the kickoff (keep the restart). Isolated to
the 2H whistle/override; KO/HT/END untouched.
**Result (reolink, vs human GT):** 05.28 2H -92->**+0s**, 06.10 2H held at **-3s** (the gap-based
60s window distinguishes 06.10's 25s restart from 05.28's 86s). Reolink within-10s 51->**52/63
(81->83%)**: 2H 13->14/18, KO/HT/END unchanged, median 1.1s. No regressions.
**Conclusion:** the 2H kickoff whistle is the first whistle once BOTH teams are back in formation,
not the first whistle while players trickle on; and "is the nearby no-whistle restart the kickoff?"
is a function of its gap to that whistle (~within a minute). Remaining full-file reolink misses:
06.06-S END +48, 06.10 KO +24, and one large KO outlier (max 335s, a truncated game).

---

## EXP-PHASE-08: 2H prefers the whistled kickoff over a preceding warm-up restart (2026-06-30)

**Hypothesis (walking 05.27 with the worklist):** 05.27 2H missed by -60s -- the detector picked a
no-whistle full restart at 54:04, but the real 2H kickoff (human GT) is 60s later at 55:04, a
full+center restart with a tightly-coupled whistle (ball 55:03, whistle 55:04). The 2H picker took
the FIRST kickoff()-passing restart in the break window, so the warm-up restart beat the whistled
kickoff right after it. The KO path already handles this (nxt_ko: a no-whistle restart immediately
followed by a whistled kickoff -> trust the whistled one); the 2H path lacked it.
**Method:** before the 2H decision, if the first break-window kickoff is no-whistle, look <=75s ahead
for a whistled full kickoff and prefer it. Isolated to the 2H selection; KO/HT/END untouched.
**Result (reolink, vs human GT):** 05.27 2H -60->**+0s**. Reolink within-10s 50->**51/63 (79->81%)**:
2H 12->13/18, KO/HT/END unchanged, median 1.3->1.1s. No regressions.
**Conclusion:** the warm-up-restart-then-whistled-kickoff pattern needed the same nxt_ko treatment in
the 2H path as in KO. Does NOT help 05.28 (real 2H 49:38 has NO detected ball restart and a stray
whistle 92s before it -- the EXP-PHASE-01 ball-in-play gap, still open).

---

## EXP-PHASE-07: 06.08 KO/END — under-counted opening kickoff + collapsed final whistle (2026-06-30)

**Hypothesis (from walking 06.08 with Mark):** 06.08 (full, non-truncated) missed both KO (+163s)
and END (+315s) while HT/2H were already +0. Mark confirmed from YouTube: the real kickoff is a
center static-ball restart (1:57) + whistle (1:59), and the final whistle at 93:57 is a clear
multi ("twit..twit.....tweeeeeeeeet") with players staying on the field afterward.
**Root cause (verified on cached signals, instant re-fuse):** (1) **KO** — the 1:59 kickoff was a
CENTER restart with a tightly-coupled whistle (1.8s) but only **5** in-field detections, under the
0.6x-median full-field gate, so it was dropped from `prek` and KO jumped to a 4:42 warm-up restart.
(2) **END** — the final multi-blow collapsed into a **single** cached blast (5639.0s = 93:59; the
whole game had only one detected "multi", at 66:22, where two blasts happened to land 0.4s apart),
and because players stay on the field after full-time the player-curve last-play `off2` sat at
end-of-file (99:12). END only anchored to multis -> fell through to `off2` = +315s.
**Method:** (1) include in `prek` a center restart with a whistle <=3s AND a MODERATE field
(>=0.4x median crowd) — accepts the under-counted real kickoff but still rejects an early warm-up
whistle over a near-empty field (05.31 Spencerport: a coincidental tight whistle at 0:30 over just
2 players is NOT the kickoff; real KO is the full restart at 1:45). (2) END: when no late multi
registers, anchor to the LAST whistle blast in the valid 2H window (refs don't whistle after
full-time, so the last blast IS the collapsed final whistle). Both changes isolated to the KO/END
branches; HT/2H/sanity-gate untouched.
**Result (reolink, vs human GT):** 06.08 KO +163->**-1s**, END +315->**+1s** (HT/2H stayed +0; all
four now within 1s). 05.31 Spencerport KO restored (warm-up regression caught + gated: 0:30 ->
**2:53-8s**). Reolink within-10s **48->50/63 (76->79%)**: KO 12->13/15, END 10->11/12, HT/2H
unchanged, median 1.3s. Zero protected-boundary regressions.
**Conclusion:** the full-field gate must flex for the opening kickoff (corroborated by a tight
whistle), and END must fall back from "multi" to "last whistle of the game" when the final
multi-blow merges into one blast. Remaining full-file reolink misses: 06.10 KO +24, the youth
warm-up-on-field 2H gap (05.28, EXP-PHASE-01), and one large KO outlier (max 335s).

---

## EXP-PHASE-06: anchor HT to the dip-preceding whistle + whistle-only 2H kickoff (2026-06-30)

**Hypothesis (Mark):** the HT-selection family (05.27/05.28) and 06.01 2H miss even though the
spot-on whistle is already a detected blast — HT leans on the player-dip (which lags the whistle by
~2min as youth players walk off slowly) and 2H requires a center ball-restart (sometimes missed by
the ball model). Both ignore the whistle. Halftime is usually a multi-blow but its spread varies
(~10s spread, or a tight tweet-tweet that the <0.4s merge collapses to one), so the 5s multi
clustering registers neither.
**Method (fusion-only, cached signals -> instant re-fuse, no re-decode):** (1) recompute wide
multis from the cached `blasts` with an 11s gap (kept the 5s `multis` for htc/END). (2) when no
central 5s-multi-with-following-dip registers, anchor HT to the whistle PRECEDING the halftime dip:
take the dip-cluster onset (merge runs split by a brief mid-break refill, <=60s gap), look back 180s
(not the old 60s snap to the lagging dip) for a wide multi, else the nearest single blast. (3) 2H =
the first whistle once the field has refilled (HT dip offset `on2`); a no-whistle ball restart in the
break window only outranks it if it is <=90s after that whistle (else it is a mid-half re-acquire,
not the kickoff). Validated on the box (`--predict --gt-only` + `phase_eval --human-only`).
**Result (reolink, vs human GT):** 05.27 HT +139->**-1s**, 05.28 HT +194->**-2s**, 06.01 2H
+379->**0s** (bonus 06.08 2H -191->**0s**). Non-truncated reolink HT within-10s **9/11 -> 11/11**;
reolink within-10s 44/63 -> **48/63 (70->76%)**, median 1.6->1.3s (HT 12->14, 2H 10->12, END max
1655->314s). No protected (non-truncated reolink) boundary regressed; KO/END/sanity-gate untouched.
**Conclusion:** anchoring HT to the dip-preceding whistle and accepting a whistle-only 2H kickoff
fixes the HT-selection family + 06.01 without disturbing the KO-signature work. Cost: heat 05.28 2H
+85->-92s (non-protected; both wrong either way — a youth warm-up-on-field game with a stray whistle
after the short dip, real 2H 3min later). Remaining: 05.28 2H and the youth warm-up-on-field 2H/END
gap (EXP-PHASE-01) still need ball-in-play, and dahua KO/2H still lack a whistle.

---

## EXP-PHASE-05: signature-driven kickoff (KO/2H) + interactive GT review (2026-06-30)

**Hypothesis (Mark):** apply the kickoff signature throughout the video — whistle + static ball at
centre + (most) players on the field, then motion — to fix KO/2H, which were grabbing wrong restarts.
**Method:** interactive review (Mark verified each off-boundary on YouTube against the trimmed-raw
upload = the YouTube video, so YT time = trimmed-file time directly). Corrected GT where wrong, then
two detector passes (commits 5026ec5, 6461a53): KO/2H = a CENTER ball-restart corroborated by a
kickoff whistle (tight to the ball ≤3s, OR part of the 2H "ready" multi-blast, OR no-whistle for
wind-masked games) AND a near-full field (relative threshold ~0.6×median in-play count, since YOLO
under-counts at distance so absolute counts can't separate one-team from both-team restarts). KO =
first pre-HT kick, 2H = first kick in the break window. Eval scores truncated games per-boundary.
**Result (reolink within-10s):** KO 6→**12/15**, 2H 5→**10/18**, HT 12/18, END 10/12, median **1.6s**.
Exact fixes: West Seneca KO +384→0, 05.27 KO +114→0, 05.30 2H -80→-1, 05.31 2H -50→-1, 06.06-S KO
-31→0, 05.28 KO -13→-1. GT errors caught: 06.06 KO (truncated), 06.10 HT (42:23→44:25, detector was
right), West Seneca END (truncated).
**Conclusion:** the kickoff signature works; the discriminating feature is a whistle TIGHT to the
ball (positioning whistles fire ~50-80s early), not the player count (too noisy at distance). Cost:
the 2H discriminator regressed 06.01 2H (+379) and the long/odd 06.08 (rejected) — to clean up next,
along with the HT-selection family (05.27/05.28/West Seneca grab the wrong halftime multi).

---

## EXP-PHASE-04: full-coverage phase eval — score ALL GT games, not reolink-only (2026-06-30)

**Hypothesis:** the prior "48% within-10s" was measured on ~13 reolink-heavy games (the detector was
reolink-only). The honest number across the full human-GT set (38 scoreable games, 25 dahua + 13 reolink)
will be lower, and will localize where the detector actually fails.
**Method:** dropped the reolink-only filter; made the dahua paths fire — `find_fullframe_video` (dahua
`combined_video` is often missing/odd), AUTO-detected frame + ball orientation (vote in-field persons/
detections both ways; `video_rotation` is inconsistent across the 2024 archive — raw files tagged rot=0),
`thread_type=AUTO` decode, no-audio crash guard, and `segment()` placeholder + central-multi HT for youth
games whose field never empties. Recomputed the per-game signal cache for all 25 dahua games on the box
(CPU, ~10 min/game; 4-core box, single worker — 2 parallel workers oversubscribe and are slower). Added a
video<->GT misalignment guard: exclude games where |voff+vdur - gt_dur| > 120 s (incomplete or multi-game
videos can't map to GT). Vision-verified orientation + that predictions land on real events.
**Result (vs human GT, 33 aligned games; 5 dahua excluded as data issues):**
- combined within-10s **33%** (43/132), median 49 s.
- **reolink 60%** (31/52), median **3 s** — UP from the old reolink "48%" (no-play robustness +
  symmetric HT/2H pair selection fixed 05.07/05.30/06.06). Near-perfect: Upper_90, 06.04, 05.07, 05.30.
- **dahua 15%** (12/80), median 128 s — HT/END decent (player-curve dip + order: 06.15 HT -1 s, 09.21
  HT -9 s, 07.02 2H -11 s), KO/2H poor (no usable whistle at 8-16 kHz; KO/2H lean on AutoCam ball
  restarts whose center-circle cluster is often mis-detected = a goal-area restart).
- 5 misaligned-video games excluded (flash 06.01 -20 min incomplete `-raw`, 10.04, 10.13 +76 min
  multi-game `combined.mp4`, heat 05.19, 05.28).
**Conclusion:** coverage solved; reolink phase detection is genuinely good (60%, median 3 s) and the
whistle is the load-bearing signal. Dahua is the open problem: with no whistle, KO/2H need a better
center-restart selection or a player-curve 2H (field-refill after the HT dip) rather than the noisy
AutoCam ball restart. The honest full-set number is 33%, not 48% — the 48% was a reolink-subset artifact.

## EXP-PHASE-03: multi-signal phase detector (player-curve + whistle), half-length AGNOSTIC (2026-06-28)

**Hypothesis (Mark):** the fixed-40-min assumption (EXP-PHASE-02) doesn't generalize — younger ages play 30-min
halves, tournaments are shorter, flash halves vary. Instead combine signals that don't assume a half length:
the WHISTLE (multi-blast = halftime/end) AND a PLAYER detector inside the field mask (the ~20 field players are
OFF the field during the halftime break). Add ball-at-center for kickoffs.
**Method (`G:\ballresearch\phase_detect.py`):** backbone = **player-on-field count curve**. Sample ~1 frame / 10s,
run `yolo26n.onnx` (COCO, person class, 1280px) and count persons whose foot-point is inside the field polygon.
Curve goes low -> high(1H) -> **low(halftime, field empties)** -> high(2H) -> low, with NO half-length assumption.
Segment: halftime = the longest SUSTAINED low-player run in the middle 20-80% of the game; 1H = first-play->halftime,
2H = halftime->last-play. **Whistle refinement** (when 44.1kHz trimmed audio exists): HT = multi-blast at the dip,
2H = first whistle once the field refills, END = largest late multi-blast with KO>=0 (the KO>=0 cap excludes
post-game whistles), KO = HT-(END-2H) [measured half] snapped to a kickoff whistle if one is right there. Curve +
whistle cached per game (`phase_cache/<gid>.json`) so the expensive pass runs once; idempotent. Sanity gate rejects
implausible fits (KO<0, break not 2.5-18min, half not 15-50min, |h1-h2|>3min) — never writes garbage. Writes
game_state source=`phase_video_whistle` + `_phase_meta`.
**Gotchas found + fixed:** (1) trimmed uploads are **1920x1080 anamorphically squeezed** from the 7680x2160 source
-> scale the field polygon by (0.25, 0.5), NOT uniform. (2) full-frame yolo@1280 over a 7680-wide panorama misses
small field players (catches only big sideline people); the 1920-wide trimmed file needs no tiling. (3) symmetry-only
half selection is fragile (EXP-PHASE-02's lesson); the player halftime-dip removes the half-length guess entirely.
(4) post-game whistles fooled END until the KO>=0 cap. (5) detached `python > log` is block-buffered -> use `-u`.
**Result — 6/04 Irondequoit (frame GT KO=2:37 HT=42:37 2H=50:16 END=90:09):** KO 2:41 (+4s), HT 42:34 (-3s),
2H 50:16 (0s), END 90:09 (0s); h1=h2=39.9. **All four within ~4s — never assuming 40min.** Batch over all 24
reolink: **2 new clean auto-phased games** beyond EXP-PHASE-02's 6 — heat 5/28 Fairport (40-min: KO 4:09/HT 44:17/
2H 48:06/END 88:13) and heat 6/06 Lakefront_Sullivan (**36-min** halves: KO 4:25/HT 41:05/2H 44:51/END 81:03) —
both vision-verified (kickoff = teams in formation, mid-halftime = empty field). The 36-min game proves the
half-agnostic claim. Sanity gate correctly REJECTED 3 fits whose "halftime" was a 1.3-1.8min stoppage (5/30 Fairport,
5/31 West_Seneca_14.40, 6/07 Lakefront_home) and flagged 6/08 Hilton_Flaitz (American-football-marked field, sparse
kickoff, 45-min/99-min — same game removed in EXP-PHASE-02). 3 games had no detectable halftime dip
(no-play-plateau: 5/30 Western_NY_Flash, 6/06 Fairport, 6/07 BU15).
**File-location fix (2026-06-28):** the detector first globbed `D:\soccer-cam-storage` and reported 6 "missing file"
games — WRONG. The canonical source is the **F: archive folder** (`F:\Heat_2012s\<date>...`, `F:\Flash_2013s\...`,
where game.json lives): named-subdir trimmed upload + `-raw` sibling + combined.mp4 + match_info.ini. Fixed
`files_offsets` to read the F: archive folder. After the fix: flash 3/21 processes (indoor-dome game, no whistle ->
player-curve-only, asymmetric -> rejected, manual). The rest were NOT real missing files: heat 5/07-16:12, flash
5/10-11:19, 5/30 Spencerport-19:43, 5/31 WSeneca-12:40 are **aborted false-starts** (combined.mp4 is 32-87s; each has
a real-game sibling or no game); heat 7/22 is a 2025 raw-segments-only recording (no trimmed/combined). So no real
2026 game is missing its file on F:.
**Conclusion:** the multi-signal detector clears the 10s bar without any half-length assumption, generalizes to
shorter-half games, and works on 16kHz games via the player curve alone (whistle-cut audio no longer fatal). It is
the general phase detector; EXP-PHASE-02's whistle+40min stays valid for the 6 heat-40 games already anchored.
**Failure mode found — HALFTIME WARM-UP ON FIELD (2026-06-28):** cross-checked the detector (--force, no write)
against the 3 manually-set `play_windows` games. The manuals confirm Mark's variable-half point: flash 5/09 ~34min,
flash 5/10 ~38min, heat 5/31 Spencerport ~30min halves (a fixed-40 detector misses all 3). The detector gets HT
(halftime START) RIGHT on all 3 (flash 5/10 42:09 vs PW 42.2min; Spencerport 31:50 vs 31.9min) but UNDER-estimates
2H and END because these teams **warm up on the field during halftime** -> the player-count dip is short (just the
initial clear-off), the "field refills" signal fires on the warm-up, so the 2nd half comes out too short. The sanity
gate REJECTED 2 of 3 (break <2.5min) but flash 5/10 slipped through as a symmetric-but-wrong "OK" (32min half vs real
38; END 12min early). **Lesson: the numeric gate is necessary but NOT sufficient — vision-verification (kickoff
formation + empty-halftime frame) is the real gate; only write what's been eyeballed.** The 2 written games (5/28,
6/06 Sullivan) were vision-verified; the 3 play_windows manuals are kept (better than the warm-up-shortened auto fit).
**Fix (needs ball signal):** distinguish halftime warm-up from real 2nd-half play via ball-in-play (ball moving /
ball-at-center kickoff), which separates scattered warm-up from coordinated play — pending the marathon's ball
detections reaching 2026 games. Until then, warm-up-on-field games stay manual.
**Next:** combined.mp4 fallback for the no-trimmed-file games (player curve works on 16kHz video; warmup at file
start needs handling); ball-at-center kickoff to fix the warm-up 2H/END + rescue the no-dip / rejected games
(post-marathon-on-2026); locate recordings for the 2 no-rec-dir games. Player counts could also be cached as a
per-game signal for the active-play training filter.

---

## EXP-PHASE-02: whistle template + 40-min-structure phase detector — <1s on all boundaries (2026-06-28)

**Hypothesis (Mark):** the referee whistle marks halftime/end; combined with the fixed half length (40 min for
Guzzetta/heat 2026) the whole phase structure can be pinned to ~seconds — far better than the detection-density model
(EXP-PHASE-01, ~4 min) or generic FFT-band whistle detection (EXP-012, ~2 min, "too noisy").
**Method (`G:\ballresearch\whistle_phases2.py`):** (1) STFT the game audio; per frame find the dominant 2-5.5kHz peak +
its tonality (band-energy / 1-8kHz). (2) The generic FFT band fails (crowd/wind), and matching the *exact* kickoff
spectrum fails (pea-whistle warble) — so detect by **pitch**: the ref uses ONE whistle pitch all game. (3) **Self-
calibrate the pitch via the 40-min lock:** try each candidate pitch; keep the one whose whistles form a 'halftime' with a
whistle ~40min BEFORE (kickoff) and a 2nd-half kickoff 4-13min after whose +40min lands on a whistle (end). Derive
kickoff = HT-40, end = 2H+40, snap to whistles.
**Result — 6/04 Irondequoit (Guzzetta), vs Mark's frame-precise GT:** winning pitch ~4250Hz (fit 3/3, 34 whistles).
kickoff 2:37.8 (GT 2:37), halftime 42:37.8 (GT 42:37, 1st half=40.00m), 2nd kickoff 50:16.2 (GT 50:16), end 90:09.2
(GT 1:30:09). **All four boundaries within ~1 second.** The kickoff whistle alone is weak (lower tonality under kickoff
crowd noise), but it's recovered by the HT-40 derivation + corroboration, so no separate kickoff detector is needed when
the structure locks.
**Conclusion:** whistle-pitch + 40-min self-calibrating structure-fit clears the 10s bar (~1s here). This is THE phase
detector for the 40-min-half games. Generalize/batch across Guzzetta 2026; for games where the kickoff/2H whistle is
missing, sharpen the derived restart with the ball-at-center cue in the tight post-derivation window (Mark's hint).
Supersedes EXP-PHASE-01 (density model) and the play_windows seeds for these games.
**Audio-rate scoping (important):** the whistle (~4350Hz) needs Nyquist > 4.4kHz. **2024-25 Dahua games have 8000Hz
audio (Nyquist 4kHz) -> the whistle band is CUT OFF**, so the whistle detector CANNOT work on them (this is exactly why
EXP-012 found whistles unreliable — it was run on 8kHz Dahua). **2026 Reolink: trimmed/upload audio 44.1kHz, combined.mp4
16kHz (Nyquist 8kHz) — whistle intact.** So: whistle+40min detector applies to **2026+ Reolink games**; the 2024-25
Dahua games keep their human game_state (they have it) or need the density/ball method.
**Batch input correction (2026-06-28):** combined.mp4 (16kHz) is UNRELIABLE for the batch — it cuts the whistle's upper
harmonics and (with pre/post-game crowd/PA audio) lets spurious low tones win the pitch search (6/04 picked a bogus
2050Hz with a negative kickoff). Inconsistent (6/15 happened to match its play_windows GT; others failed). **Use the
TRIMMED upload file (44.1kHz, game-only)** — reproduces 6/04 exactly (4250Hz; 2:37/42:37/50:16/90:09, all ~1s). Map
trimmed-time -> (seg,f) via `match_info.ini start_time_offset` (varies per game: 6/04=6:00, 6/15=1:00). Batch tool:
`G:\ballresearch\whistle_batch2.py` (writes game_state source=whistle_40min + `_whistle_meta`{trimmed_times,offset,pitch,
score} for traceability; writes only clean 40-min fits, logs the rest).
**Batch outcome (heat-2026, 19 games):** **7 written** with whistle-anchored game_state — 6/04 (4250, GT-validated),
5/07_18.28 (3750), 5/27 (3750), 6/01 (3850), 6/08 (3450), 6/10 (3950), 6/15 (3550, via raw-file fallback). Two refinements
proved out: (1) **pitch constraint to 3-4.8kHz** — 6/08 had a spurious 2250Hz lock; constrained, it found the real 3450Hz
whistle; 5/28's 2050Hz had no in-range whistle -> honest no-fit. (2) **raw.mp4 fallback** (44.1k/16k full recording,
offset 0) rescued 6/15 (its trimmed upload was a corrupt 52MB stub). **9 no-fit + 3 no-trimmed-file** — no-fits failed at
BOTH trimmed(44.1k) and raw(16k) in 3-4.8kHz, so it's poor ref-mic audio or non-40-min sub-games, NOT a pitch issue.
Those need the marathon's detections (ball-at-center kickoff fallback, once it reaches 2026 games) or manual phase-editor
entry. Net: whistle detector delivers sub-second phases for the games with a clean ref-whistle; ~37% of heat-2026 here.
**Auto-half-length is UNRELIABLE (don't pursue):** tried sweeping the half length (25-45min) for non-40-min leagues
(flash ~34-38min). On 6/04 it picked **half=27min** (perfectly symmetric spurious whistle pair, sym=0) over the correct
40min (sym=6s) — symmetry-only scoring rewards any coincidental symmetric pair. The half length must be a KNOWN league
constant (heat/Guzzetta 2026 = 40min, confirmed). flash halves vary per game (34-38) so even a fixed flash-half won't fit
cleanly -> flash-2026 phases need ball-at-center kickoff (post-marathon) or manual phase-editor. Whistle+fixed-40 is the
solution for the heat/Guzzetta 2026 games only.
**Vision-verification pass (2026-06-28):** decoded the frame at each detected kickoff for the 6 non-GT anchored games.
**5 confirmed game-in-progress** at kickoff (5/07_18.28, 5/27, 6/01, 6/10, 6/15) + 6/04 (frame-precise GT) = **6 verified
whistle-anchored games**. **6/08 REMOVED** — its detected kickoff (0:19, score-2, 21-min offset) was an empty
football-marked field; reverted to needs-manual. Lesson: low fit score + odd offset = "don't trust"; require score 3 +
non-empty kickoff frame.

---

## EXP-PHASE-01: train a game-phase detector on human phases (detection features) — ~4m MAE, marginal (2026-06-27)

**Hypothesis:** Mark's 27 human `game_state` sets (now in `game.json`, aligned (seg,f) space) can supervise a phase
detector using `autocam_detections` features (no upload offset / fps drift, unlike play_windows).
**Method:** box-scratch `G:\ballresearch\train_phase_model.py`. 19 games with human phases + detections. Per 10s window:
top-1 conf, #high-conf candidates/frame, x-spread of high-conf (multi-ball), in-field ratio, top-1 motion, temporal
position. numpy multinomial logistic-regression emission + **duration-constrained segmental DP** (learned per-phase
min/max + Gaussian length prior). Leave-one-game-out. (Whistle dropped — corrupt-audio decode + EXP-012 already showed
it's noisy.)
**Result (LOO MAE, minutes):** kickoff 5.2 · HT-start 4.8 · 2H-start 5.2 · **game-end 2.3** · overall **4.4** (4.1 with
per-boundary calibration). game-end within-2m 12/19; kickoff/HT/2H ~5/19. Stable across feature/DP/calibration variants.
**Conclusion:** Confirms EXP-012 — detection signal is **not sharp** at warmup→1st-half and the halftime edges (ball
activity looks similar across them); **game-end is reliable** (activity stops). A trained density model alone plateaus
~4m. EXP-012's better 2.1m needed whistle + asymmetric density + crowd-energy + calibration, and still only 10/29 games
within 2m on all boundaries. **Practical use:** model is a recording-time SEED for the phase editor (better than the
offset play_windows seeds; game-end trustworthy), NOT accurate enough to auto-fill phases unverified. Phase editor
(`/static/phase-edit.html`) remains the reliable path.
**Follow-up (10s-precision attempt):** to hit a 10-second bar, tested a coarse-to-fine **kickoff fine-localizer** —
"ball at field-center, still, then burst of motion" from `autocam_detections` top-1, in an ORACLE +/-5min window around
the human kickoff (`G:\ballresearch\kickoff_localize.py`). **FAILED: 172s MAE, 1/19 within 10s.** Raw top-1 detections
are too noisy to catch the placed-then-struck ball, and center-restarts (every goal kickoff) are ambiguous even in the
window. **Conclusion: 10s automated precision is NOT reachable from the ball-detection signal.** The only plausible
automated path is a trained **visual** kickoff/whistle model (big build, GPU, uncertain it reaches 10s — EXP-012 already
put sub-1m as hard). For true 10s precision, **human scrubbing in the phase editor is the reliable answer**
(frame-precise); auto-detection can only pre-seed game-end (~2m).
**Follow-up 2 (multi-blast-whistle hypothesis, 6 games):** tested "multi-blast = halftime/end" + temporal window
(`G:\ballresearch\whistle_test.py`, FFT whistle clusters w/ blast counts vs human HT/end). **Hypothesis NOT confirmed:**
the nearest **3+ multi-blast** cluster is **18-21 min** from the true HT/end (median 1125s/1295s) — i.e. 3+ clusters are
NOISE bursts in the 2-4.5kHz band (crowd/coaches/wind), they do NOT mark periods. BUT the nearest whistle cluster of
**any** count IS near the boundary: HT median 33s (4/6 within 60s, most ~23-33s), END median 43s (4/6 within 60s). So
whistle = a **coarse ~30-60s anchor**, not a 10s one, and blast-count doesn't discriminate (matches EXP-012's noise
finding). **Caveat:** the ~30s "error" may be partly human-label imprecision (manifest.db phases scrubbed to ~thumbnail
granularity) — if human GT is only ~30s-precise, 10s is unmeasurable against it and the whistle may already be at GT
precision. Untested refinement: isolate the period-end whistle by **duration/loudness** (long ref blast) not blast-count.

---


## EXP-008: Field-boundary distillation pipeline (2026-06-11)

**Hypothesis:** A small in-house CNN can reproduce the teacher's 10-point field polygon closely enough — IoU ≥ 0.90 vs teacher, gate agreement ≥ 90%, per-point error ≤ ~8px in 768×384 — to replace it as a drop-in ONNX.
**Method:** Standalone distillation — label-gen (teacher over Reolink footage) → placement-split dataset + heavy augmentation → ResNet18 dual-head student → ONNX export matching the teacher's I/O signature + parity check. Corpus: ~33 Reolink games (7680×2160) from `D:/soccer-cam-storage`, ~9 venues, Heat-heavy plus a few Flash; Dahua footage excluded.
**Result (2026-06-12, GPU server):** Generated 1,000 teacher labels over 21 Reolink games → 8 placement clusters (1 Flash + 7 Heat), split train=688 / val=66 / test=246. ResNet18 student, early-stopped epoch 87 (best epoch 72, val pixel error 15.1 px ≈ 2% of 768 width). Held-out **test** (davis + hilton): overall IoU 0.64, gate-agree 0.84 — but per-cluster: **davis_park IoU 0.79**, **hilton_high_school IoU 0.32**. Export parity vs teacher on representative frames: **IoU 0.936, gate-agree 1.00**, mean per-point delta 20.6 px; ONNX signature byte-identical to teacher, drop-in through unmodified `field_detector.py` verified (20/20), checkpoint-vs-ONNX deviation 0.25 px.
**Conclusion:** Distillation works on normal grass venues (davis 0.79, representative-frame parity 0.94) — a viable v1 drop-in. The 0.90 bar is not met *overall* because hilton_high_school is an **American-football turf field** (yard lines, glare) where the teacher itself is unreliable (mean_score 0.43, zero gate-pass frames) — out-of-distribution, not a model defect. Per-point: near-center (pt 2) best (9.7 px), intermediates/corners worst (pt 1: 32.9 px). Next: exclude football-field venues or add human-corrected labels for anomalous venues (v2); more venues would raise the floor. Winning backbone: resnet18.
**Artifacts:** `F:/training_checkpoints/field_outline/student.onnx`, run `training/runs/field_kpts_v1/`.
**Code:** `training/field_outline/`, `training/cli/*_field_outline.py`

## EXP-007: Game phase detection from multi-ball patterns (2026-03-30)

**Hypothesis:** Warmup/halftime/postgame have multiple scattered ball detections; active play has a single ball trajectory.
**Method:** `game_phase_detector.py` — 30-second rolling windows, count frames with >3 concurrent detections spread >500px apart. Phase transitions at multi-ball/single-ball boundaries.
**Result:** Generated manifests for 9 games. Most games show clear warmup→first_half→halftime→second_half→postgame progression.
**Conclusion:** Works for standard games. FAILS for tournaments — sub-game breaks detected as single long halftime. Multi-game recordings need per-sub-game phase detection.
**Data:** `F:/training_data/game_manifests/{game_id}.json`

## EXP-006: Far-field gap detection across all rows (2026-03-30)

**Hypothesis:** ONNX trajectory gaps (missing detections between linked positions) exist in r1/r2 too, not just r0.
**Method:** `exp_allrow_gaps.py` — trajectory linking + gap detection on all tile rows.
**Result:** 19,239 gaps total. r0: 6,967, r1: 9,491, r2: 2,781. r1 has the most gaps.
**Conclusion:** Gap filling should target all rows, not just far-field. r1 (mid-field) is the biggest opportunity.
**Data:** `F:/training_data/experiments/exp_allrow_gaps.json`
**Code:** `training/experiments/exp_allrow_gaps.py`

## EXP-005: Targeted frame diff at gap positions (2026-03-29)

**Hypothesis:** Frame differencing at ONNX gap positions (where ball should be but wasn't detected) will find missed balls with fewer false positives than blind frame diff.
**Method:** `exp3b_fullscale.py` — seek to each gap frame in video, extract small region around predicted position, check for motion blob matching ball size/circularity.
**Result:** 4,570 verified motion candidates, 1,565 high-confidence (size 15-200px², circularity >0.5, on-field).
**Conclusion:** Gap-guided targeting dramatically reduces false positives vs blind frame diff. 797 Sonnet-verified as real balls.
**Code:** `training/experiments/exp3b_fullscale.py`

## EXP-004: ONNX trajectory gap mining (2026-03-29)

**Hypothesis:** When ONNX detects a ball in r0 in frames N and N+2 but not N+1, the ball is likely still there in N+1 — the model just missed it.
**Method:** `exp1_onnx_gaps.py` — trajectory linking on r0 labels, find frames where detections are missing between linked positions, interpolate expected position.
**Result:** 11,425 gap candidates across 9 games (avg 1,269/game).
**Conclusion:** Gaps are real and frequent. Most gaps are 1-3 frames — brief occlusions or model uncertainty. Provides high-quality training targets.
**Data:** `F:/training_data/experiments/exp1_onnx_gaps.json`
**Code:** `training/experiments/exp1_onnx_gaps.py`

## EXP-003: Blind frame differencing for small balls (2026-03-28)

**Hypothesis:** Motion-based detection (frame differencing) can find small balls that ONNX misses at far-field distances.
**Method:** `frame_diff_detector.py` — compute frame diff on r0 tiles, filter by circularity >0.5 and area 15-300px², link into trajectories (min 3 frames, path >30px).
**Result:** 31,000 "moving" trajectories in 200 frames — overwhelmingly player motion, not balls.
**Conclusion:** FAILED as standalone approach. Player motion dominates. Needs: (1) player mask subtraction, (2) ONNX gap guidance to focus search, (3) tighter circularity/size filters.
**Follow-up:** EXP-005 used gap-guided targeting and succeeded.
**Code:** `training/data_prep/frame_diff_detector.py`

## EXP-002: Sonnet Vision QA for label quality (2026-03-27)

**Hypothesis:** Sonnet can reliably verify whether a tile crop contains a soccer ball.
**Method:** `label_qa_prep.py` generates 3x2 composite grids of tile crops. Sonnet classifies each as BALL/NOT_BALL. Batched at ~100/hr to stay within budget.
**Result:** 4,042 positive tiles reviewed: 33.4% true positive, 29% false positive. 4,333 negative tiles: 0.6% false negative rate.
**Conclusion:** Sonnet is excellent at confirming negatives (99.4% accuracy) and good at catching false positives. Positions r1_c5, r1_c6, r2_c4 have highest FP rates (sun glare, poor detection).
**Data:** `F:/training_data/label_qa/report.json`

## EXP-001: Tracker parameter sweep (2026-03-25)

**Hypothesis:** Optimal Kalman filter parameters for ball tracking can be found via systematic sweep.
**Method:** `review_packets/tracking_lab/experiment_log.md` — sweep gate distance (50-500px), max_miss frames (10-120), process noise, on one game segment.
**Result:** Best: gate=300, max_miss=90 achieved 95.2% coverage (frames with tracked ball / total frames).
**Conclusion:** Large gate + high persistence works for panoramic view where ball can move fast between frames. Prediction quality matters more than tight gating.
**Data:** `review_packets/tracking_lab/experiment_log.md`

## EXP-000: Label filtering heuristics (2026-03-22)

**Hypothesis:** Simple geometry filters can remove obvious false detections from ONNX bootstrap labels.
**Method:** `label_filters.py` — aspect ratio 0.5-2.0, width 0.008-0.06 normalized, edge clipping.
**Result:** 568K → 488K files, 759K → 606K detections (20% removed).
**Follow-up:** Trajectory validator removed additional 24% (606K → 462K), keeping only detections in trajectories ≥3 frames.
**Code:** `training/data_prep/label_filters.py`, `training/data_prep/trajectory_validator.py`
