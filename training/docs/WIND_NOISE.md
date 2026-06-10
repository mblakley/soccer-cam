# Wind-noise reduction — investigation results (2026-06)

Reducing the constant wind "scratching" on the outdoor camera microphone while
preserving sideline/coach speech. Harness: `training/experiments/wind_noise_reduction.py`.
Status: **investigation parked** — not yet wired into the pipeline. Best working
point identified is a real but **modest** improvement on genuinely wind-buried audio.

## TL;DR

- Wind noise is **two** problems: low **rumble** (<200 Hz) and a broadband
  **"blowout"/gust scratch** (~1.5–8 kHz). A high-pass removes the rumble but
  **not** the scratch — that's the part you actually hear.
- **Deep speech-enhancement models (DeepFilterNet, RNNoise) are the wrong tool.**
  They are trained to *reconstruct speech*, so on mostly-wind audio they
  **hallucinate fake-voice artifacts**. Rejected by ear despite good
  background-removal (BAK) scores.
- Best **non-generative** chain: **high-pass (rumble) + ffmpeg `afftdn` (gust)
  + optional tuned non-stationary `noisereduce` (quieter background)** — the
  `hybrid_*` methods.
- It's a genuine **tradeoff**: a quieter background always costs some voice
  quality (musical-noise). `noisereduce`'s `prop_decrease` is the dial; the
  conservative-threshold "safe" variants did **not** help.
- Chosen working point: **`hybrid_p80`** = high-pass 120 + `afftdn` str35 +
  non-stationary `noisereduce` prop≈0.80. ~17–48× realtime (~2–5 min CPU per
  90-min game). Audition verdict: differences between settings are small;
  `hybrid_p80` is the best of them but only a minor improvement.

## Method & metric

The harness loudness-matches every candidate on the 300–3400 Hz speech band (so
they're judged on tone/noise, not level) and reports, per candidate:

- **per-band energy removed** vs original: `rumble` (20–200 Hz), `mid`
  (200–1500 Hz), `gust` (1500–7900 Hz).
- **DNSMOS** (no-reference perceptual MOS): `SIG` (speech), `BAK` (background),
  `OVRL`. Caveat: DNSMOS is **unreliable on near-voiceless wind** (it favors
  speech quality); trust it only on clips that actually contain speech, and
  corroborate by ear + spectrogram.

All DSP runs **in-process via PyAV** (libavfilter), no subprocess ffmpeg.

## Test material

- `windy_30s.mp4` — a 30 s slice chosen for **physical camera wobble** (the air
  was buffeting the camera); nearly voiceless, good for the wind-removal axis.
- **`combined_windtalk.wav`** (156 s) — five wind+talk windows cut from the
  2026-06-06 game (`2026.06.06-15.01.33`), where wind buries **close coach
  speech**. Used for the speech-preservation axis. Speech was so buried that a
  Silero VAD detected only ~0.8 min of 82; the talking is there, just masked.

## Final sweep — combined wind+talk clip, ranked by DNSMOS OVRL

| setting | rumble-Δ | mid-Δ | gust-Δ | SIG (voice) | BAK (quiet) | speed |
|---|--:|--:|--:|--:|--:|--:|
| original | 0 | 0 | 0 | 2.20 | 2.20 | — |
| afftdn + hp120 (no nr) | 14 | 0.1 | 0.7 | **2.41** | 2.79 | 48× |
| hybrid p65 | 21 | 5.1 | 3.6 | 2.34 | 2.95 | 26× |
| **hybrid p80** | 24 | 6.8 | 4.6 | 2.28 | 3.11 | 17× |
| hybrid p90 | 26 | 8.1 | 5.3 | 2.18 | 3.28 | 22× |
| noisereduce full (p1.0) | 22 | 9.6 | 6.1 | 2.13 | **3.53** | 15× |

Monotonic voice-vs-quiet tradeoff: as `prop_decrease` rises, BAK (quiet) goes up
and SIG (voice) comes down. The afftdn stage preserves the speech bands
(mid ≈ 0) but doesn't move the SIG/BAK frontier vs plain noisereduce; it mainly
cleans the gust in ways DNSMOS underweights.

## Things that did NOT work / dead ends

- **High-pass alone** — kills boom, leaves the scratch (low OVRL despite big
  sub-200 Hz reduction).
- **`afftdn` with fixed noise floor / noise-tracking** at default strength —
  ~0 dB on the gust until strength (`nr`) is raised; even then ~5–6 dB is the
  clean ceiling (pushing harder adds artifacts, not removal).
- **DeepFilterNet / RNNoise** — fake-voice hallucination on wind (see TL;DR).
- **`anlmdn`** — negligible effect on wind.
- **noisereduce conservative threshold** (`thresh_n_mult_nonstationary=2.5`,
  the `*_safe` variants) — identical SIG to the plain variants; not a useful knob.
- **A passing motorcycle** masqueraded as "wind" rumble in an early test clip —
  verify the low-frequency source before tuning.

## Reproduce

```bash
# non-generative sweep on one clip, ranked by DNSMOS (needs the dev/ml extra for scipy)
uv run --with noisereduce --with speechmos --with librosa --with "numpy<2" --with soundfile \
  python -m training.experiments.wind_noise_reduction \
  --clip-file path/to/clip.wav --output-dir out/ \
  --only aff35_hp120,hybrid_p65,hybrid_p80,hybrid_p90
```

Outputs numbered WAVs + spectrogram PNGs + `RANKING.md` in `out/`.
`--no-rank` keeps definition order (for parameter sweeps); `--no-mos` skips DNSMOS.

## If revisited

- Accept `hybrid_p80` as a modest improvement and wire it into the pipeline as an
  optional audio-cleanup step (audio is already re-encoded to AAC at
  convert/combine time, so the added cost is small).
- Try `noisereduce` mask-smoothing (`freq_mask_smooth_hz` / `time_mask_smooth_ms`)
  to soften the musical-noise at a given background level.
- The fundamental limit is that the wind genuinely **buries** close speech; no
  tested method fully recovers it without artifacts.
