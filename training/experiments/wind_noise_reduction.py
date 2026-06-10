"""Wind-noise reduction experiment harness (soccer-cam).

Investigation harness (not wired into the pipeline) for reducing the wind
rumble + broadband "blowout"/scratch on an outdoor camera microphone while
preserving sideline/coach speech. Given a recording directory (auto-picks the
windiest voiced ~60 s window) or an explicit --clip-file, it runs the clip
through a battery of denoising methods, loudness-matches every result so they
are auditioned fairly, scores each per frequency band plus an optional
no-reference perceptual MOS (DNSMOS), and writes numbered WAVs + spectrogram
PNGs + a RANKING.md.

Methods span three families:
  * classic DSP        -- Butterworth high-pass (rumble), ffmpeg afftdn /
                          anlmdn (broadband gust), run in-process via PyAV.
  * spectral gating    -- noisereduce (stationary + non-stationary).
  * deep speech models -- RNNoise, DeepFilterNet (incl. attenuation-limited).

Findings (full write-up: training/docs/WIND_NOISE.md):
  * The low rumble is a high-pass job; the audible "scratch" is broadband
    gust energy that a high-pass alone does NOT remove.
  * Deep speech-enhancement models (DeepFilterNet/RNNoise) reconstruct speech
    and HALLUCINATE fake-voice artifacts on mostly-wind audio -- rejected by
    ear despite good background-removal scores.
  * Best non-generative result: high-pass (rumble) + afftdn (gust) + optional
    tuned non-stationary noisereduce (the `hybrid_*` methods) for a quieter
    background. On genuinely wind-buried audio the gain is modest.

Metric: per-band energy removed -- rumble (20-200 Hz), mid (200-1500 Hz),
gust (1500-7900 Hz) -- plus DNSMOS SIG (speech) / BAK (background) / OVRL.

Base deps: av + numpy (core) and scipy (the project's dev/ml extra --
`uv sync --extra dev`). Deep-learning + DNSMOS deps are pulled ephemerally so
pyproject.toml is untouched. DNSMOS (speechmos) needs numpy<2 (numba/librosa);
the deep models need torch. Examples:

  # non-generative sweep on one clip, ranked by DNSMOS
  uv run --with noisereduce --with speechmos --with librosa --with "numpy<2" \
         --with soundfile \
         python -m training.experiments.wind_noise_reduction \
         --clip-file path/to/clip.wav --output-dir out/ \
         --only aff35_hp120,hybrid_p65,hybrid_p80,hybrid_p90

  # full sweep (incl. deep models) on the windiest window of a recording dir
  uv run --with noisereduce --with deepfilternet --with torchaudio \
         --with pyrnnoise --with speechmos --with librosa --with "numpy<2" \
         --with soundfile \
         python -m training.experiments.wind_noise_reduction \
         --input-dir shared_data/<recording>

Every method is isolated in try/except: a lib that fails to install or run is
skipped and logged, never killing the batch.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from scipy import signal

LOG = logging.getLogger("wind")

SR = 16000  # all internal processing is mono 16 kHz (the camera's native rate)
WIND_HI = 200  # Hz; wind/rumble band ceiling
SPEECH_LO, SPEECH_HI = 300, 3400  # telephone speech band (voices, whistles, calls)

# Defaults are repo-relative; run from the repo root, or pass --input-dir / --clip-file.
DEFAULT_INPUT = Path("shared_data")
DEFAULT_OUTPUT = Path("shared_data/_experiment/wind_noise")


# --------------------------------------------------------------------------- #
# Audio I/O (PyAV, mirrors video_grouper/utils/ffmpeg_utils.py patterns)
# --------------------------------------------------------------------------- #
def decode_audio_mono16k(path: Path) -> np.ndarray | None:
    """Decode a file's first audio stream to a mono 16 kHz float32 array in [-1, 1]."""
    try:
        with av.open(str(path)) as container:
            astreams = [s for s in container.streams if s.type == "audio"]
            if not astreams:
                return None
            astream = astreams[0]
            resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
            chunks: list[np.ndarray] = []
            for frame in container.decode(astream):
                for rframe in resampler.resample(frame):
                    chunks.append(rframe.to_ndarray().reshape(-1))
            for rframe in resampler.resample(None):  # flush
                chunks.append(rframe.to_ndarray().reshape(-1))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("decode failed for %s: %s", path.name, exc)
        return None
    if not chunks:
        return None
    return np.concatenate(chunks).astype(np.float32)


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    y = np.clip(x, -1.0, 1.0)
    pcm = (y * 32767.0).astype(np.int16)
    try:
        import soundfile as sf

        sf.write(str(path), pcm, sr, subtype="PCM_16")
    except Exception:  # noqa: BLE001 -- fall back to scipy
        from scipy.io import wavfile

        wavfile.write(str(path), sr, pcm)


# --------------------------------------------------------------------------- #
# Spectral helpers
# --------------------------------------------------------------------------- #
def _stft_power(x: np.ndarray, sr: int, nfft: int = 1024, hop: int = 256):
    if len(x) < nfft:
        x = np.pad(x, (0, nfft - len(x)))
    f, _, Z = signal.stft(x, fs=sr, nperseg=nfft, noverlap=nfft - hop, window="hann")
    return np.abs(Z), f  # mag shape (freq, time)


def band_energy(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    mag, f = _stft_power(x, sr)
    power = (mag**2).mean(axis=1)  # avg power per freq bin
    mask = (f >= lo) & (f < hi)
    return float(power[mask].sum()) + 1e-12


def speech_band_rms(x: np.ndarray, sr: int) -> float:
    sos = signal.butter(
        4, [SPEECH_LO, SPEECH_HI], btype="bandpass", fs=sr, output="sos"
    )
    b = signal.sosfiltfilt(sos, x)
    return float(np.sqrt(np.mean(b**2) + 1e-12))


def normalize_to_speech_rms(x: np.ndarray, sr: int, target_rms: float) -> np.ndarray:
    """Match speech-band loudness so variants are judged on tone/noise, not level."""
    cur = speech_band_rms(x, sr)
    if cur < 1e-9:
        return x.astype(np.float32)
    y = x * (target_rms / cur)
    peak = float(np.max(np.abs(y))) + 1e-9
    if peak > 0.99:
        y = y * (0.99 / peak)
    return y.astype(np.float32)


def save_spectrogram(x: np.ndarray, sr: int, path: Path) -> None:
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return
    mag, _ = _stft_power(x, sr, nfft=1024, hop=256)
    db = 20.0 * np.log10(mag + 1e-6)
    db = np.clip(db, -80.0, 0.0)
    img = (255.0 * (db - db.min()) / (db.max() - db.min() + 1e-9)).astype(np.uint8)
    img = np.flipud(img)  # low frequencies at the bottom
    im = Image.fromarray(img)
    w = min(1400, max(400, img.shape[1]))
    im = im.resize((w, 360))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(str(path))


# --------------------------------------------------------------------------- #
# Resampling for 48 kHz-native deep models
# --------------------------------------------------------------------------- #
def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x.astype(np.float32)
    g = math.gcd(sr_in, sr_out)
    return signal.resample_poly(x, sr_out // g, sr_in // g).astype(np.float32)


# --------------------------------------------------------------------------- #
# DSP primitives
# --------------------------------------------------------------------------- #
def highpass(x: np.ndarray, sr: int, fc: float, order: int = 4) -> np.ndarray:
    sos = signal.butter(order, fc, btype="highpass", fs=sr, output="sos")
    return signal.sosfiltfilt(sos, x).astype(np.float32)


def ffmpeg_filtergraph(x: np.ndarray, sr: int, chain: str) -> np.ndarray:
    """Run a libavfilter audio chain in-process via PyAV (no subprocess ffmpeg)."""
    graph = av.filter.Graph()
    src = graph.add_abuffer(
        sample_rate=sr, format="fltp", layout="mono", time_base=Fraction(1, sr)
    )
    prev = src
    for spec in chain.split(","):
        name, _, argstr = spec.strip().partition("=")
        node = graph.add(name.strip(), argstr.strip() or None)
        prev.link_to(node)
        prev = node
    sink = graph.add("abuffersink")
    prev.link_to(sink)
    graph.configure()

    frame = av.AudioFrame.from_ndarray(
        x.reshape(1, -1).astype(np.float32), format="fltp", layout="mono"
    )
    frame.sample_rate = sr
    frame.pts = 0
    frame.time_base = Fraction(1, sr)
    src.push(frame)
    src.push(None)

    out: list[np.ndarray] = []
    while True:
        try:
            oframe = sink.pull()
        except (av.error.BlockingIOError, av.error.EOFError, EOFError):
            break
        except av.FFmpegError:
            break
        out.append(oframe.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) if out else x.astype(np.float32)


# --------------------------------------------------------------------------- #
# Method implementations -- each takes (x16, noise16) -> y16, may raise
# --------------------------------------------------------------------------- #
def m_highpass(fc):
    return lambda x, noise: highpass(x, SR, fc)


def m_afftdn(x, noise):
    return ffmpeg_filtergraph(x, SR, "afftdn=nf=-25")


def m_anlmdn(x, noise):
    return ffmpeg_filtergraph(x, SR, "anlmdn=s=0.0008:p=0.003:r=0.008")


def m_hp_afftdn(x, noise):
    return ffmpeg_filtergraph(highpass(x, SR, 120), SR, "afftdn=nf=-25")


def m_hp_afftdn_anlmdn(x, noise):
    y = ffmpeg_filtergraph(
        highpass(x, SR, 120), SR, "afftdn=nf=-25,anlmdn=s=0.0008:p=0.003:r=0.008"
    )
    return y


def m_nr_stationary(x, noise):
    import noisereduce as nr

    return nr.reduce_noise(
        y=x, sr=SR, stationary=True, y_noise=noise, prop_decrease=0.95, n_fft=1024
    ).astype(np.float32)


def m_nr_ns_light(x, noise):
    import noisereduce as nr

    return nr.reduce_noise(
        y=x,
        sr=SR,
        stationary=False,
        prop_decrease=0.75,
        time_constant_s=2.0,
        n_fft=1024,
    ).astype(np.float32)


def m_nr_ns_aggressive(x, noise):
    import noisereduce as nr

    return nr.reduce_noise(
        y=x, sr=SR, stationary=False, prop_decrease=1.0, time_constant_s=2.0, n_fft=1024
    ).astype(np.float32)


def m_hp_nr_ns(x, noise):
    import noisereduce as nr

    xf = highpass(x, SR, 120)
    return nr.reduce_noise(
        y=xf,
        sr=SR,
        stationary=False,
        prop_decrease=0.9,
        time_constant_s=2.0,
        n_fft=1024,
    ).astype(np.float32)


def m_rnnoise(x, noise):
    from pyrnnoise import RNNoise

    x48 = resample(x, SR, 48000)
    pcm = np.clip(x48, -1, 1)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    den = RNNoise(48000)
    out_frames: list[np.ndarray] = []
    # pengzhendong/pyrnnoise: denoise_chunk yields (speech_prob, denoised_int16_frame)
    for item in den.denoise_chunk(pcm16):
        frame = item[1] if isinstance(item, tuple) else item
        out_frames.append(np.asarray(frame).reshape(-1))
    y48 = np.concatenate(out_frames).astype(np.float32) / 32767.0
    return resample(y48, 48000, SR)


def _shim_torchaudio_backend():
    """DeepFilterNet 0.5.6 imports torchaudio.backend.common.AudioMetaData, removed in
    the modern torchaudio required for Python 3.13. Re-expose it (used only as a type
    hint) so df.io imports; enhance() runs on in-memory tensors, no file I/O needed."""
    import sys
    import types

    import torchaudio

    try:
        from torchaudio.backend.common import AudioMetaData  # noqa: F401

        return
    except Exception:  # noqa: BLE001
        amd = getattr(torchaudio, "AudioMetaData", None) or type(
            "AudioMetaData", (), {}
        )
        backend = sys.modules.get("torchaudio.backend") or types.ModuleType(
            "torchaudio.backend"
        )
        common = types.ModuleType("torchaudio.backend.common")
        common.AudioMetaData = amd
        backend.common = common
        sys.modules["torchaudio.backend"] = backend
        sys.modules["torchaudio.backend.common"] = common
        torchaudio.backend = backend


_DF_STATE: dict = {}  # lazy cache for the DeepFilterNet model (loaded once per process)


def _deepfilternet(x, atten_lim_db=None):
    import torch

    _shim_torchaudio_backend()
    from df.enhance import enhance, init_df

    if "model" not in _DF_STATE:
        model, df_state, _sr = init_df()
        _DF_STATE.update(model=model, df_state=df_state)
    x48 = resample(x, SR, 48000)
    audio = torch.from_numpy(x48).unsqueeze(0)
    kwargs = {"atten_lim_db": atten_lim_db} if atten_lim_db is not None else {}
    enhanced = enhance(_DF_STATE["model"], _DF_STATE["df_state"], audio, **kwargs)
    y48 = enhanced.squeeze(0).detach().cpu().numpy().astype(np.float32)
    return resample(y48, 48000, SR)


def m_deepfilternet(x, noise):
    return _deepfilternet(x)


def m_hp_deepfilternet(x, noise):
    return _deepfilternet(highpass(x, SR, 120))


def m_df_atten(atten, pre_hp=None):
    """DeepFilterNet capped at atten_lim_db dB of suppression (keeps ambience natural)."""

    def fn(x, noise):
        xx = highpass(x, SR, pre_hp) if pre_hp else x
        return _deepfilternet(xx, atten_lim_db=atten)

    return fn


def m_hp_nr_df(x, noise):
    """Gentle 3-stage cascade: each step removes a little, minimizing artifacts."""
    import noisereduce as nr

    y = highpass(x, SR, 100)
    y = nr.reduce_noise(
        y=y, sr=SR, stationary=False, prop_decrease=0.6, time_constant_s=2.0, n_fft=1024
    ).astype(np.float32)
    return _deepfilternet(y, atten_lim_db=24)


def m_afftdn_tracked(x, noise):
    return ffmpeg_filtergraph(x, SR, "afftdn=nr=25:nf=-30:tn=1")


def m_hp_afftdn_tracked(x, noise):
    return ffmpeg_filtergraph(highpass(x, SR, 120), SR, "afftdn=nr=25:nf=-30:tn=1")


def m_nr_aggr_hp150(x, noise):
    import noisereduce as nr

    y = nr.reduce_noise(
        y=x, sr=SR, stationary=False, prop_decrease=1.0, time_constant_s=2.0, n_fft=1024
    ).astype(np.float32)
    return highpass(y, SR, 150)


def m_hybrid(nr_prop=0.7, nr_thresh=2.0, hp=120, aff_nr=35):
    """Quiet-background hybrid: high-pass (rumble) -> afftdn (gust) -> non-stationary
    noisereduce (residual steady background). `nr_prop` = how hard noisereduce subtracts,
    `nr_thresh` = noise threshold multiplier (higher = more conservative, protects voice)."""

    def fn(x, _noise):
        import noisereduce as nr

        y = highpass(x, SR, hp)
        y = ffmpeg_filtergraph(y, SR, f"afftdn=nr={aff_nr}:nf=-25:tn=1")
        return nr.reduce_noise(
            y=y,
            sr=SR,
            stationary=False,
            prop_decrease=nr_prop,
            time_constant_s=2.0,
            n_fft=1024,
            thresh_n_mult_nonstationary=nr_thresh,
        ).astype(np.float32)

    return fn


def m_afftdn_hp(nr=25, nf=-25, tn=1, hp=None, hp_first=True, post_anlmdn=False):
    """Two-stage non-generative wind reduction: afftdn for the broadband 'blowout' gust
    + a high-pass for the low rumble. `nr` = afftdn strength (dB), `hp` = rumble cutoff (Hz),
    `hp_first` controls order, `post_anlmdn` adds a gentle transient smoother."""
    chain = f"afftdn=nr={nr}:nf={nf}:tn={tn}"

    def fn(x, _noise):
        y = x
        if hp and hp_first:
            y = highpass(y, SR, hp)
        y = ffmpeg_filtergraph(y, SR, chain)
        if post_anlmdn:
            y = ffmpeg_filtergraph(y, SR, "anlmdn=s=0.001:p=0.004:r=0.01")
        if hp and not hp_first:
            y = highpass(y, SR, hp)
        return y

    return fn


METHODS = [
    ("highpass_080", "High-pass 80 Hz (gentle rumble cut)", m_highpass(80)),
    ("highpass_120", "High-pass 120 Hz", m_highpass(120)),
    ("highpass_150", "High-pass 150 Hz", m_highpass(150)),
    ("highpass_200", "High-pass 200 Hz (aggressive)", m_highpass(200)),
    ("afftdn", "ffmpeg afftdn (FFT spectral denoise)", m_afftdn),
    ("anlmdn", "ffmpeg anlmdn (non-local means denoise)", m_anlmdn),
    ("hp120_afftdn", "High-pass 120 + afftdn", m_hp_afftdn),
    ("hp120_afftdn_anlmdn", "High-pass 120 + afftdn + anlmdn", m_hp_afftdn_anlmdn),
    (
        "noisereduce_stationary",
        "noisereduce stationary (wind profile)",
        m_nr_stationary,
    ),
    ("noisereduce_ns_light", "noisereduce non-stationary (light)", m_nr_ns_light),
    (
        "noisereduce_ns_aggr",
        "noisereduce non-stationary (aggressive)",
        m_nr_ns_aggressive,
    ),
    ("hp120_noisereduce_ns", "High-pass 120 + noisereduce non-stationary", m_hp_nr_ns),
    ("rnnoise", "RNNoise (Xiph RNN denoise)", m_rnnoise),
    ("deepfilternet", "DeepFilterNet (deep speech enhancement)", m_deepfilternet),
    ("hp120_deepfilternet", "High-pass 120 + DeepFilterNet", m_hp_deepfilternet),
    # --- combinations (added) ---
    ("df_atten10", "DeepFilterNet (atten-limit 10 dB, natural)", m_df_atten(10)),
    ("df_atten20", "DeepFilterNet (atten-limit 20 dB)", m_df_atten(20)),
    (
        "hp120_df_atten20",
        "High-pass 120 + DeepFilterNet (atten-limit 20 dB)",
        m_df_atten(20, pre_hp=120),
    ),
    (
        "hp100_nr_df",
        "High-pass 100 + noisereduce(light) + DeepFilterNet(atten 24)",
        m_hp_nr_df,
    ),
    ("afftdn_tracked", "ffmpeg afftdn (noise-tracking mode)", m_afftdn_tracked),
    (
        "hp120_afftdn_tracked",
        "High-pass 120 + afftdn (noise-tracking)",
        m_hp_afftdn_tracked,
    ),
    ("nr_aggr_hp150", "noisereduce aggressive + High-pass 150", m_nr_aggr_hp150),
    # --- afftdn(gust) x high-pass(rumble) sweep: non-generative two-stage ---
    (
        "aff35_gust_only",
        "afftdn gust-cut str35 (no rumble cut)",
        m_afftdn_hp(nr=35, hp=None),
    ),
    (
        "aff45_gust_only",
        "afftdn gust-cut str45 (no rumble cut)",
        m_afftdn_hp(nr=45, nf=-30, hp=None),
    ),
    (
        "aff35_hp80",
        "afftdn str35 → high-pass 80 (gentle rumble cut)",
        m_afftdn_hp(nr=35, hp=80, hp_first=False),
    ),
    (
        "aff35_hp120",
        "afftdn str35 → high-pass 120 (moderate rumble cut)",
        m_afftdn_hp(nr=35, hp=120, hp_first=False),
    ),
    (
        "aff35_hp200",
        "afftdn str35 → high-pass 200 (aggressive rumble cut)",
        m_afftdn_hp(nr=35, hp=200, hp_first=False),
    ),
    (
        "aff45_hp120",
        "afftdn str45 → high-pass 120 (strong gust + rumble cut)",
        m_afftdn_hp(nr=45, nf=-30, hp=120, hp_first=False),
    ),
    (
        "hp120_then_aff35",
        "high-pass 120 → afftdn str35 (rumble first, then gust)",
        m_afftdn_hp(nr=35, hp=120, hp_first=True),
    ),
    (
        "aff35_hp120_anlmdn",
        "afftdn str35 + anlmdn → high-pass 120 (extra transient smoothing)",
        m_afftdn_hp(nr=35, hp=120, hp_first=False, post_anlmdn=True),
    ),
    # --- hybrid: hp(rumble)+afftdn(gust)+tuned noisereduce(background) — quiet bg, protect voice ---
    (
        "hybrid_p50",
        "hp120 + afftdn35 + noisereduce p0.50 (gentle bg)",
        m_hybrid(nr_prop=0.50),
    ),
    ("hybrid_p65", "hp120 + afftdn35 + noisereduce p0.65", m_hybrid(nr_prop=0.65)),
    ("hybrid_p80", "hp120 + afftdn35 + noisereduce p0.80", m_hybrid(nr_prop=0.80)),
    (
        "hybrid_p90",
        "hp120 + afftdn35 + noisereduce p0.90 (quiet bg)",
        m_hybrid(nr_prop=0.90),
    ),
    (
        "hybrid_p80_safe",
        "hp120 + afftdn35 + noisereduce p0.80, conservative thresh (protect voice)",
        m_hybrid(nr_prop=0.80, nr_thresh=2.5),
    ),
    (
        "hybrid_p90_safe",
        "hp120 + afftdn35 + noisereduce p0.90, conservative thresh (protect voice)",
        m_hybrid(nr_prop=0.90, nr_thresh=2.5),
    ),
]


# --------------------------------------------------------------------------- #
# Segment selection
# --------------------------------------------------------------------------- #
def windiness_curves(x: np.ndarray, sr: int):
    """Per-second low-band energy fraction and speech-band RMS."""
    win = sr
    n = len(x) // win
    wind, speech = [], []
    for i in range(n):
        seg = x[i * win : (i + 1) * win]
        low = band_energy(seg, sr, 20, WIND_HI)
        total = band_energy(seg, sr, 20, sr // 2) + 1e-12
        wind.append(low / total)
        speech.append(speech_band_rms(seg, sr))
    return np.asarray(wind), np.asarray(speech)


def pick_segment(audio_by_file: dict[str, np.ndarray], clip_s: int):
    """Choose the windiest clip that still contains voices, plus a noise-only window."""
    best = None  # (score, name, start_sample, x_clip)
    best_noise = None  # (wind_frac, x_noise)
    for name, x in audio_by_file.items():
        if x is None or len(x) < clip_s * SR:
            continue
        wind, speech = windiness_curves(x, SR)
        if len(wind) < clip_s:
            continue
        speech_med = np.median(speech)
        # slide a clip_s window by 5 s; want high wind AND speech present
        for start in range(0, len(wind) - clip_s + 1, 5):
            w = wind[start : start + clip_s].mean()
            s = speech[start : start + clip_s].mean()
            if s < speech_med:  # require above-median voice activity
                continue
            score = w * s
            if best is None or score > best[0]:
                clip = x[start * SR : (start + clip_s) * SR]
                best = (score, name, start, clip)
        # noise profile: 3 s window, windiest with least speech
        speech_lo = np.percentile(speech, 25)
        for start in range(0, len(wind) - 3 + 1):
            if speech[start : start + 3].mean() > speech_lo:
                continue
            wfrac = wind[start : start + 3].mean()
            if best_noise is None or wfrac > best_noise[0]:
                best_noise = (wfrac, x[start * SR : (start + 3) * SR])
    return best, best_noise


def pick_noise_window(x: np.ndarray, sr: int = SR, win_s: int = 3) -> np.ndarray:
    """Windiest, least-voiced N-second window of x — a noise profile for spectral methods."""
    wind, speech = windiness_curves(x, sr)
    if len(wind) < win_s:
        return x[: win_s * sr]
    speech_lo = np.percentile(speech, 25)
    best = None
    for start in range(0, len(wind) - win_s + 1):
        if speech[start : start + win_s].mean() > speech_lo:
            continue
        wfrac = wind[start : start + win_s].mean()
        if best is None or wfrac > best[0]:
            best = (wfrac, x[start * sr : (start + win_s) * sr])
    return best[1] if best else x[: win_s * sr]


# --------------------------------------------------------------------------- #
# DNSMOS (best-effort, no-reference perceptual MOS)
# --------------------------------------------------------------------------- #
def dnsmos_scores(x16: np.ndarray):
    try:
        from speechmos import dnsmos

        r = dnsmos.run(x16.astype(np.float32), sr=SR)
        return {
            "OVRL": float(r.get("ovrl_mos", float("nan"))),
            "SIG": float(r.get("sig_mos", float("nan"))),
            "BAK": float(r.get("bak_mos", float("nan"))),
        }
    except Exception as exc:  # noqa: BLE001
        LOG.debug("dnsmos unavailable: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Wind-noise reduction experiment")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--clip-seconds", type=int, default=60)
    parser.add_argument("--max-files", type=int, default=0, help="0 = scan all")
    parser.add_argument(
        "--only", type=str, default="", help="comma list of method keys"
    )
    parser.add_argument(
        "--rescan", action="store_true", help="re-select the clip (ignore cache)"
    )
    parser.add_argument(
        "--clip-file",
        type=Path,
        default=None,
        help="use this exact clip (whole file); skip directory scan/cache",
    )
    parser.add_argument(
        "--project-minutes",
        type=float,
        default=90.0,
        help="game length used for the 'est. full game' time projection",
    )
    parser.add_argument(
        "--no-rank",
        action="store_true",
        help="keep methods in definition order (for a parameter sweep) instead of ranking",
    )
    parser.add_argument(
        "--no-mos",
        action="store_true",
        help="skip DNSMOS scoring (faster; metric is unreliable on near-voiceless wind)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_dir = out_dir / "spectrograms"
    spec_dir.mkdir(parents=True, exist_ok=True)

    cache_path = out_dir / "_clip_cache.npz"
    if args.clip_file:
        x_orig = decode_audio_mono16k(args.clip_file)
        if x_orig is None:
            LOG.error("no audio stream in %s", args.clip_file)
            return 1
        noise = pick_noise_window(x_orig)
        src_name = args.clip_file.name
        start_s = 0
        LOG.info(
            "using clip-file: %s (%.1fs whole clip; noise profile %.1fs)",
            src_name,
            len(x_orig) / SR,
            len(noise) / SR,
        )
    elif cache_path.exists() and not args.rescan:
        d = np.load(cache_path, allow_pickle=True)
        x_orig = d["x_orig"].astype(np.float32)
        noise = d["noise"].astype(np.float32)
        src_name = str(d["src_name"])
        start_s = int(d["start_s"])
        total_audio_s = float(d["total_audio_s"])
        LOG.info(
            "reusing cached clip: %s @ %ds (%ds); pass --rescan to reselect",
            src_name,
            start_s,
            args.clip_seconds,
        )
    else:
        files = sorted(p for p in args.input_dir.glob("*.mp4"))
        if args.max_files:
            files = files[: args.max_files]
        LOG.info("scanning %d files for the windiest voiced segment...", len(files))

        audio_by_file: dict[str, np.ndarray] = {}
        for p in files:
            t0 = time.time()
            x = decode_audio_mono16k(p)
            if x is not None:
                audio_by_file[p.name] = x
                LOG.info(
                    "  decoded %s (%.0fs audio) in %.1fs",
                    p.name,
                    len(x) / SR,
                    time.time() - t0,
                )

        if not audio_by_file:
            LOG.error("no audio found in %s", args.input_dir)
            return 1

        total_audio_s = sum(len(x) for x in audio_by_file.values()) / SR

        best, best_noise = pick_segment(audio_by_file, args.clip_seconds)
        if best is None:
            LOG.error("could not find a suitable voiced+windy segment")
            return 1
        _, src_name, start_s, x_orig = best
        noise = best_noise[1] if best_noise else x_orig[: 3 * SR]
        np.savez(
            cache_path,
            x_orig=x_orig,
            noise=noise,
            src_name=src_name,
            start_s=start_s,
            total_audio_s=total_audio_s,
        )
        LOG.info(
            "chosen clip: %s @ %ds (%ds), noise-profile %.1fs (cached for reuse)",
            src_name,
            start_s,
            args.clip_seconds,
            len(noise) / SR,
        )

    clip_dur_s = len(x_orig) / SR
    project_s = args.project_minutes * 60.0

    target_rms = speech_band_rms(x_orig, SR)
    e_rumble_orig = band_energy(
        x_orig, SR, 20, 200
    )  # low wind rumble (high-pass's job)
    e_mid_orig = band_energy(x_orig, SR, 200, 1500)  # body
    e_gust_orig = band_energy(
        x_orig, SR, 1500, 7900
    )  # broadband 'blowout'/scratch (afftdn's job)

    only = {k.strip() for k in args.only.split(",") if k.strip()}
    methods = [m for m in METHODS if not only or m[0] in only]

    # original is variant 0 (reference)
    results = []  # dict per variant
    orig_norm = normalize_to_speech_rms(x_orig, SR, target_rms)
    score_mos = (lambda _a: None) if args.no_mos else dnsmos_scores
    orig_entry = {
        "key": "original",
        "label": "ORIGINAL (untouched reference)",
        "norm": orig_norm,
        "rumble_db": 0.0,
        "mid_db": 0.0,
        "gust_db": 0.0,
        "composite": -1e9,
        "dnsmos": score_mos(orig_norm),
        "elapsed": 0.0,
        "rt": 0.0,
    }

    # Warm up model-loading methods so timing reflects steady-state per-clip cost
    # (the one-time model load/JIT is excluded from the per-clip timing).
    if any(
        ("deepfilternet" in k or "_df" in k or k.startswith("df_"))
        for k, _, _ in methods
    ):
        try:
            m_deepfilternet(x_orig[:SR], noise)
            LOG.info("warmed up DeepFilterNet model (load excluded from timing)")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("DeepFilterNet warmup failed: %s", exc)

    for key, label, fn in methods:
        try:
            t0 = time.perf_counter()
            y = fn(x_orig, noise)
            elapsed = time.perf_counter() - t0  # noise-reduction time only
            y = np.asarray(y, dtype=np.float32).reshape(-1)
            if len(y) < SR:  # sanity
                raise ValueError(f"output too short ({len(y)} samples)")
            rumble_db = 10.0 * math.log10(e_rumble_orig / band_energy(y, SR, 20, 200))
            mid_db = 10.0 * math.log10(e_mid_orig / band_energy(y, SR, 200, 1500))
            gust_db = 10.0 * math.log10(e_gust_orig / band_energy(y, SR, 1500, 7900))
            composite = rumble_db + mid_db + gust_db
            norm = normalize_to_speech_rms(y, SR, target_rms)
            rt = clip_dur_s / max(elapsed, 1e-6)  # x realtime
            results.append(
                {
                    "key": key,
                    "label": label,
                    "norm": norm,
                    "rumble_db": rumble_db,
                    "mid_db": mid_db,
                    "gust_db": gust_db,
                    "composite": composite,
                    "dnsmos": score_mos(norm),
                    "elapsed": elapsed,
                    "rt": rt,
                }
            )
            LOG.info(
                "  OK  %-28s rumble-%.1f mid-%.1f gust-%.1f dB  %.2fs (%.0fx RT)",
                key,
                rumble_db,
                mid_db,
                gust_db,
                elapsed,
                rt,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("  SKIP %-24s %s: %s", key, type(exc).__name__, exc)

    # rank: DNSMOS OVRL if available for all, else composite
    have_mos = all(r["dnsmos"] for r in results)

    def sort_key(r):
        if have_mos:
            return (r["dnsmos"]["OVRL"], r["composite"])
        return (r["composite"],)

    if not args.no_rank:
        results.sort(key=sort_key, reverse=True)

    # clear stale numbered outputs from previous runs (ranking renumbers files)
    for old in list(out_dir.glob("[0-9][0-9]_*.wav")) + list(
        spec_dir.glob("[0-9][0-9]_*.png")
    ):
        try:
            old.unlink()
        except OSError:
            pass

    # write WAVs (00 = original, then ranked) + spectrograms
    write_wav(out_dir / "00_original.wav", orig_entry["norm"])
    save_spectrogram(orig_entry["norm"], SR, spec_dir / "00_original.png")
    for i, r in enumerate(results, start=1):
        write_wav(out_dir / f"{i:02d}_{r['key']}.wav", r["norm"])
        save_spectrogram(r["norm"], SR, spec_dir / f"{i:02d}_{r['key']}.png")

    # RANKING.md
    lines = [
        "# Wind-noise reduction — ranked results",
        "",
        f"- Source: `{src_name}` @ {start_s}s, {clip_dur_s:.0f}s clip (mono 16 kHz)",
        f"- All clips loudness-matched on the 300–3400 Hz speech band (target RMS {target_rms:.4f}).",
        f"- Order: {'definition order (parameter sweep)' if args.no_rank else ('DNSMOS OVRL' if have_mos else 'composite band-energy')}.",
        "",
        "All Δ = dB of energy **removed** vs original (higher = more removed):  ",
        "**rumble-Δ** = 20–200 Hz (low wind rumble — the high-pass's job).  ",
        "**mid-Δ** = 200–1500 Hz (body / most voice).  ",
        "**gust-Δ** = 1500–7900 Hz (broadband 'blowout' / scratch — the afftdn's job).  ",
        "DNSMOS (if shown): SIG=speech, BAK=background, OVRL=overall (1–5, higher better).  ",
        f"**sec/{clip_dur_s:.0f}s** = CPU time to denoise this {clip_dur_s:.0f}s clip (single process). "
        f"**×RT** = faster-than-realtime factor.  ",
        f"**est. game** = projected time to process a {args.project_minutes:.0f}-min game "
        f"(model-load excluded; linear extrapolation).",
        "",
        f"| # | file | method | rumble-Δ | mid-Δ | gust-Δ | SIG | BAK | OVRL | sec/{clip_dur_s:.0f}s | ×RT | est. game |",
        "|--:|------|--------|--------:|-----:|------:|----:|----:|-----:|--------:|----:|----------:|",
    ]

    scale = project_s / clip_dur_s

    def mos_cell(r, k):
        return f"{r['dnsmos'][k]:.2f}" if r["dnsmos"] else "—"

    def time_cells(r):
        if r.get("elapsed", 0) <= 0:
            return "—", "—", "—"
        return (
            f"{r['elapsed']:.2f}",
            f"{r['rt']:.0f}×",
            f"{r['elapsed'] * scale / 60:.1f} min",
        )

    s, rt, est = time_cells(orig_entry)
    lines.append(
        f"| 00 | `00_original.wav` | {orig_entry['label']} | 0.0 | 0.0 | 0.0 | "
        f"{mos_cell(orig_entry, 'SIG')} | {mos_cell(orig_entry, 'BAK')} | {mos_cell(orig_entry, 'OVRL')} | "
        f"{s} | {rt} | {est} |"
    )
    for i, r in enumerate(results, start=1):
        s, rt, est = time_cells(r)
        lines.append(
            f"| {i:02d} | `{i:02d}_{r['key']}.wav` | {r['label']} | "
            f"{r['rumble_db']:.1f} | {r['mid_db']:.1f} | {r['gust_db']:.1f} | "
            f"{mos_cell(r, 'SIG')} | {mos_cell(r, 'BAK')} | {mos_cell(r, 'OVRL')} | "
            f"{s} | {rt} | {est} |"
        )
    lines += [
        "",
        "Listen 00 → NN (00 = untouched reference).",
        "Goal: kill the broadband gust/scratch AND the low rumble, without artifacts.",
        "Speed vs. quality: prefer the cheapest setting whose sound you're happy with —",
        "the ×RT / est. columns show what each costs per game.",
        "",
    ]
    (out_dir / "RANKING.md").write_text("\n".join(lines), encoding="utf-8")

    LOG.info("done -> %s  (%d variants + original)", out_dir, len(results))
    LOG.info("RANKING.md written; listen 00..%02d in order", len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
