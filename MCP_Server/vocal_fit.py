"""Offline key/tuning/tempo analysis for fit_vocal.

Pure numpy/scipy (no librosa). Vocals are monophonic, so the key comes from
a YIN pitch track (pitch-class histogram weighted by loudness) instead of a
polyphonic chroma — more robust against breaths, reverb tails and noise.

The shift to the song is NOT chosen via "detected key vs song key" (vocal
key detection often confuses relative major/minor). Instead every shift in
-6..+5 semitones is scored by how much of the vocal's pitch energy lands on
the song's scale tones; the best score wins, ties go to the smallest shift.
"""

import numpy as np
from scipy.signal import resample_poly

from .audio_analysis import load_mono

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Krumhansl-Kessler key profiles (only used to NAME the detected key).
_KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "locrian": [0, 1, 3, 5, 6, 8, 10],
    "harmonic minor": [0, 2, 3, 5, 7, 8, 11],
    "melodic minor": [0, 2, 3, 5, 7, 9, 11],
    "minor pentatonic": [0, 3, 5, 7, 10],
    "major pentatonic": [0, 2, 4, 7, 9],
}

_SR = 16000


def parse_key(text):
    """'F# minor' / 'Am' / 'Eb major' / 'D dorian' -> (root 0-11, intervals, label)."""
    t = text.strip().replace("♯", "#").replace("♭", "b")
    if not t:
        raise ValueError("empty key")
    root_txt = t[0].upper()
    rest = t[1:]
    if rest[:1] in ("#", "b"):
        root_txt += rest[0]
        rest = rest[1:]
    flats = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}
    root_txt = flats.get(root_txt, root_txt)
    if root_txt not in NOTE_NAMES:
        raise ValueError("unknown root in key '%s'" % text)
    mode = rest.strip().lower() or "major"
    if mode in ("m", "min", "moll"):
        mode = "minor"
    if mode in ("maj", "dur"):
        mode = "major"
    if mode not in SCALES:
        raise ValueError("unknown scale '%s' (known: %s)" % (mode, ", ".join(SCALES)))
    root = NOTE_NAMES.index(root_txt)
    return root, SCALES[mode], "%s %s" % (root_txt, mode)


def _yin_track(x, sr, fmin=70.0, fmax=1000.0, win=512, thresh=0.15):
    """Vectorised YIN. Returns (midi float per frame or nan, frame rms)."""
    tau_min = int(sr / fmax)
    tau_max = int(sr / fmin)
    hop = 128
    flen = win + tau_max
    n = 1 + (len(x) - flen) // hop
    if n <= 0:
        return np.array([]), np.array([])
    idx = np.arange(flen)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx]
    nfft = 1 << int(np.ceil(np.log2(flen + win)))
    A = np.fft.rfft(frames[:, :win], nfft)
    B = np.fft.rfft(frames, nfft)
    r = np.fft.irfft(np.conj(A) * B, nfft)[:, :tau_max + 1]
    sq = np.concatenate([np.zeros((n, 1)), np.cumsum(frames ** 2, axis=1)], axis=1)
    e0 = sq[:, win][:, None]
    taus = np.arange(tau_max + 1)
    et = sq[:, taus + win] - sq[:, taus]
    d = np.maximum(e0 + et - 2 * r, 0.0)
    cm = np.cumsum(d[:, 1:], axis=1)
    dn = np.ones_like(d)
    dn[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(cm, 1e-12)

    rms = np.sqrt(e0[:, 0] / win)
    midi = np.full(n, np.nan)
    sub = dn[:, tau_min:tau_max]
    below = sub < thresh
    has = below.any(axis=1)
    first = np.where(has, below.argmax(axis=1), sub.argmin(axis=1))
    # walk to the local minimum after the first threshold crossing
    for i in np.nonzero(has | (sub.min(axis=1) < 0.25))[0]:
        j = first[i]
        while j + 1 < sub.shape[1] and sub[i, j + 1] < sub[i, j]:
            j += 1
        t = j + tau_min
        if 1 <= t < tau_max:
            a, b, c = dn[i, t - 1], dn[i, t], dn[i, t + 1]
            den = a - 2 * b + c
            t = t + (0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0)
        f = sr / t
        midi[i] = 69 + 12 * np.log2(f / 440.0)
    return midi, rms


def analyze_vocal(path):
    """Pitch-class profile, tuning offset and key name of a (mono) vocal."""
    x, sr = load_mono(path)
    if sr != _SR:
        g = np.gcd(int(sr), _SR)
        x = resample_poly(x, _SR // g, int(sr) // g)
    x = x - np.mean(x)
    midi, rms = _yin_track(x, _SR)
    if midi.size == 0:
        raise ValueError("file too short for pitch analysis")
    gate = np.max(rms) * 10 ** (-35 / 20)
    voiced = ~np.isnan(midi) & (rms > gate)
    # drop single-frame octave glitches: keep frames close to their neighbours
    m = midi.copy()
    m[~voiced] = np.nan
    jump = np.abs(np.diff(m, prepend=np.nan)) > 1.0
    jump2 = np.abs(np.diff(m, append=np.nan)) > 1.0
    stable = voiced & ~(jump & jump2)
    nv = int(stable.sum())
    if nv < 40:
        raise ValueError("almost no pitched vocal found (%d frames) — spoken/whispered/noise?" % nv)

    pm = m[stable]
    w = rms[stable]
    cents = (pm - np.round(pm)) * 100.0
    # weighted circular mean of the cent deviation = tuning offset
    ang = cents / 100.0 * 2 * np.pi
    tuning = float(np.angle(np.sum(w * np.exp(1j * ang))) / (2 * np.pi) * 100.0)
    pc_float = (pm - tuning / 100.0)
    pcs = np.mod(np.round(pc_float), 12).astype(int)
    hist = np.bincount(pcs, weights=w, minlength=12)
    hist = hist / hist.sum()

    keys = []
    for root in range(12):
        for name, prof in (("major", _KK_MAJOR), ("minor", _KK_MINOR)):
            c = float(np.corrcoef(hist, np.roll(prof, root))[0, 1])
            keys.append((c, "%s %s" % (NOTE_NAMES[root], name)))
    keys.sort(reverse=True)
    lo, hi = np.percentile(pm, [5, 95])
    return {
        "key": keys[0][1],
        "key_confidence": round(keys[0][0], 3),
        "alternatives": [{"key": k, "score": round(c, 3)} for c, k in keys[1:4]],
        "tuning_offset_cents": round(tuning, 1),
        "pitch_class_profile": {NOTE_NAMES[i]: round(float(hist[i]), 3) for i in range(12)},
        "range": "%s%d–%s%d" % (NOTE_NAMES[int(round(lo)) % 12], int(round(lo)) // 12 - 1,
                                 NOTE_NAMES[int(round(hi)) % 12], int(round(hi)) // 12 - 1),
        "voiced_seconds": round(nv * 128 / _SR, 1),
        "_hist": hist,
    }


def best_shift(hist, root, intervals, max_shift=6):
    """Score every shift by the vocal energy landing in the target scale."""
    target = np.zeros(12)
    target[[(root + i) % 12 for i in intervals]] = 1.0
    tonic_w = np.zeros(12)
    tonic_w[root] = 1.0
    tonic_w[(root + 7) % 12] = 0.5
    rows = []
    for s in range(-max_shift, max_shift):
        h = np.roll(hist, s)
        rows.append({"semitones": s,
                     "in_scale": round(float(h @ target), 3),
                     "tonic_weight": round(float(h @ tonic_w), 3)})
    rows.sort(key=lambda r: (-r["in_scale"], -r["tonic_weight"], abs(r["semitones"])))
    return rows


def estimate_bpm(path, lo=70.0, hi=180.0):
    """Rough tempo from onset-strength autocorrelation (cross-check only).

    Comb over 1..4 beat multiples + fine lag search, so 125 vs 130 BPM is
    resolvable; no tempo prior beyond a mild preference against extremes.
    """
    x, sr = load_mono(path)
    if sr != _SR:
        g = np.gcd(int(sr), _SR)
        x = resample_poly(x, _SR // g, int(sr) // g)
    hop, win = 64, 1024
    n = 1 + (len(x) - win) // hop
    if n < 200:
        return None
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    S = np.log1p(np.abs(np.fft.rfft(x[idx] * np.hanning(win), axis=1)))
    flux = np.maximum(np.diff(S, axis=0), 0).sum(axis=1)
    flux = flux - flux.mean()
    nf = 1 << int(np.ceil(np.log2(2 * len(flux))))
    F = np.fft.rfft(flux, nf)
    ac = np.fft.irfft(F * np.conj(F), nf)[:len(flux)]
    fps = _SR / hop
    bpms = np.arange(lo, hi + 0.01, 0.1)
    score = np.zeros_like(bpms)
    for k in (1, 2, 3, 4):
        lag = k * 60 * fps / bpms
        ok = lag < len(ac) - 1
        score[ok] += np.interp(lag[ok], np.arange(len(ac)), ac)
    score *= np.exp(-0.5 * (np.log2(bpms / 120.0) / 1.0) ** 2)
    return round(float(bpms[np.argmax(score)]), 1)
