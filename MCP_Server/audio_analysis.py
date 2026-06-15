"""Offline audio analysis for AbletonMCP.

Audio never flows through the Remote Script socket (the Live Object Model
gives no access to sample buffers), so all spectral work happens here on
rendered files. Workflow: export or Freeze+Flatten a track to a WAV/AIFF,
analyse it with these functions, then translate the findings into device
changes inside Live via the set_device_parameter tool.

Pure functions only (no MCP / no socket) so they stay easy to test.
"""

import os

import numpy as np
import soundfile as sf

# numpy 2.0 renamed trapz -> trapezoid; support both.
_trapz = getattr(np, "trapezoid", None) or np.trapz

# Named frequency bands (Hz) for human-readable reports.
BANDS = [
    ("sub", 20.0, 60.0),
    ("bass", 60.0, 250.0),
    ("low_mid", 250.0, 500.0),
    ("mid", 500.0, 2000.0),
    ("high_mid", 2000.0, 6000.0),
    ("high", 6000.0, 20000.0),
]


def load_mono(path):
    """Read an audio file and downmix to a mono float64 array.

    Tries libsndfile (WAV/AIFF/FLAC/OGG) first; falls back to audioread+ffmpeg
    for compressed formats (m4a/mp3/aac) that libsndfile can't open.
    """
    try:
        data, sr = sf.read(path, always_2d=True)
        return data.mean(axis=1).astype(np.float64), int(sr)
    except RuntimeError:
        try:
            import audioread
        except ImportError:
            raise RuntimeError(
                "Could not decode '%s'. Compressed formats (m4a/mp3) need the "
                "'audioread' package and ffmpeg installed." % path)
        with audioread.audio_open(path) as f:
            sr, channels = int(f.samplerate), int(f.channels)
            raw = b"".join(f.read_data())
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        if channels > 1:
            x = x.reshape(-1, channels).mean(axis=1)
        return x, sr


def integrated_lufs(signal, sr):
    """Integrated loudness (LUFS, ITU-R BS.1770) via pyloudnorm if available.

    Returns None if pyloudnorm isn't installed or the clip is too short to
    measure, so the rest of the report still works.
    """
    try:
        import pyloudnorm as pyln
    except Exception:
        return None
    try:
        meter = pyln.Meter(sr)
        return round(float(meter.integrated_loudness(signal)), 2)
    except Exception:
        return None


def average_psd(signal, sr, frame=4096, hop=2048):
    """Welch-style averaged power spectral density.

    Averaging |FFT|^2 over overlapping Hann-windowed frames gives a far more
    stable spectrum estimate than one giant FFT of the whole signal.
    Returns (freqs, psd) with uniformly spaced freqs.
    """
    if len(signal) < frame:
        signal = np.pad(signal, (0, frame - len(signal)))

    window = np.hanning(frame)
    win_power = np.sum(window ** 2)

    psd_accum = np.zeros(frame // 2 + 1)
    count = 0
    for start in range(0, len(signal) - frame + 1, hop):
        seg = signal[start:start + frame] * window
        spectrum = np.fft.rfft(seg)
        psd_accum += (np.abs(spectrum) ** 2) / (sr * win_power)
        count += 1

    if count == 0:  # signal exactly one frame after padding
        seg = signal[:frame] * window
        psd_accum = (np.abs(np.fft.rfft(seg)) ** 2) / (sr * win_power)
        count = 1

    psd = psd_accum / count
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    return freqs, psd


def _band_energy(freqs, psd):
    """Energy fraction (and dB) per named band, relative to total energy."""
    total = _trapz(psd, freqs) or 1e-12
    out = {}
    for name, lo, hi in BANDS:
        mask = (freqs >= lo) & (freqs < hi)
        e = _trapz(psd[mask], freqs[mask]) if mask.any() else 0.0
        frac = float(e / total)
        out[name] = {
            "range_hz": [lo, hi],
            "energy_fraction": round(frac, 4),
            "energy_db": round(float(10 * np.log10(frac + 1e-12)), 2),
        }
    return out


def _dominant_frequency(freqs, psd, lo=20.0, hi=20000.0):
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():
        return 0.0
    return float(freqs[mask][int(np.argmax(psd[mask]))])


def analyze(path, start_s=None, end_s=None):
    """Full single-file report: levels, LUFS, dominant freq, centroid, bands.

    Pass start_s/end_s (seconds) to analyse only a section — e.g. the drop vs
    a verse — instead of averaging over the whole file.
    """
    sig, sr = load_mono(path)

    # Optional time window.
    window = None
    if start_s is not None or end_s is not None:
        a = int(max(0.0, start_s or 0.0) * sr)
        b = int((end_s * sr)) if end_s is not None else len(sig)
        b = min(max(a, b), len(sig))
        sig = sig[a:b]
        window = {"start_s": round(a / sr, 3), "end_s": round(b / sr, 3)}

    freqs, psd = average_psd(sig, sr)
    rms = float(np.sqrt(np.mean(sig ** 2))) if len(sig) else 0.0
    peak = float(np.max(np.abs(sig))) if len(sig) else 0.0
    centroid = float(np.sum(freqs * psd) / (np.sum(psd) + 1e-12))

    result = {
        "path": path,
        "sample_rate": sr,
        "duration_s": round(len(sig) / sr, 3) if sr else 0.0,
        "window": window,
        "peak_dbfs": round(20 * np.log10(peak + 1e-12), 2),
        "rms_dbfs": round(20 * np.log10(rms + 1e-12), 2),
        "integrated_lufs": integrated_lufs(sig, sr),
        "dominant_hz": round(_dominant_frequency(freqs, psd), 1),
        "spectral_centroid_hz": round(centroid, 1),
        "bands": _band_energy(freqs, psd),
    }
    return result


def find_masking(path_low, path_high, fmax=500.0):
    """Find where two stems compete in the low end (spectral masking).

    Tuned for the classic kick (low) vs bass (high) clash but works for any
    pair. PSDs are normalised to compare *shape* not absolute level, then
    compared on a common low-frequency grid up to ``fmax``.

    Returns each source's dominant frequency, an overlap score, the most
    masked band, and a recommended high-pass frequency for the 'high' source
    (the frequency up to which the 'low' source dominates the shared range).
    """
    siga, sra = load_mono(path_low)
    sigb, srb = load_mono(path_high)
    fa, pa = average_psd(siga, sra)
    fb, pb = average_psd(sigb, srb)

    grid = np.linspace(20.0, fmax, 512)
    pa_i = np.interp(grid, fa, pa)
    pb_i = np.interp(grid, fb, pb)

    # Normalise each PSD to its own peak -> compare spectral shape.
    pa_n = pa_i / (pa_i.max() + 1e-12)
    pb_n = pb_i / (pb_i.max() + 1e-12)
    overlap = np.minimum(pa_n, pb_n)

    kick_peak = float(grid[int(np.argmax(pa_i))])
    bass_peak = float(grid[int(np.argmax(pb_i))])

    # Where the low source still dominates the shared range by a margin:
    # high-pass the high source just above that to free up the sub for the kick.
    dominance = pa_n - pb_n
    dom_mask = dominance > 0.05
    crossover = float(grid[dom_mask].max()) if dom_mask.any() else kick_peak

    band_overlap = {}
    for name, lo, hi in BANDS:
        if lo >= fmax:
            continue
        m = (grid >= lo) & (grid < min(hi, fmax))
        band_overlap[name] = round(float(_trapz(overlap[m], grid[m])), 4) if m.any() else 0.0
    worst = max(band_overlap, key=band_overlap.get) if band_overlap else None

    return {
        "low_source": path_low,
        "high_source": path_high,
        "low_dominant_hz": round(kick_peak, 1),
        "high_dominant_hz": round(bass_peak, 1),
        "overlap_score": round(float(_trapz(overlap, grid)), 4),
        "most_masked_band": worst,
        "band_overlap": band_overlap,
        "recommended_highpass_hz": round(crossover, 1),
        "recommendation": (
            "Low source dominant ~%.0f Hz, high source dominant ~%.0f Hz; they "
            "overlap most in the '%s' band. Try a high-pass on the HIGH source "
            "around %.0f Hz (or a narrow EQ cut there) so the LOW source keeps "
            "the sub. Always verify by ear." % (kick_peak, bass_peak, worst, crossover)
        ),
    }


def analyze_mix(paths, fmax=2000.0):
    """Analyse many stems at once and rank every masking conflict between them.

    For each stem: level + dominant frequency. For each PAIR: a normalised
    spectral-overlap score, the most masked band, and which stem to high-pass
    (the one whose energy sits *above* the other's in the shared low end) at
    what frequency. Conflicts are returned sorted worst-first so the caller
    can fix the biggest problems before the small ones.
    """
    grid = np.linspace(20.0, fmax, 1024)

    stems = []
    norms = []
    for p in paths:
        sig, sr = load_mono(p)
        freqs, praw = average_psd(sig, sr)
        psd = np.interp(grid, freqs, praw)
        norms.append(psd / (psd.max() + 1e-12))

        rms = float(np.sqrt(np.mean(sig ** 2))) if len(sig) else 0.0
        peak = float(np.max(np.abs(sig))) if len(sig) else 0.0
        stems.append({
            "name": os.path.basename(p),
            "path": p,
            "peak_dbfs": round(20 * np.log10(peak + 1e-12), 2),
            "rms_dbfs": round(20 * np.log10(rms + 1e-12), 2),
            "dominant_hz": round(float(grid[int(np.argmax(psd))]), 1),
        })

    conflicts = []
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            overlap = np.minimum(norms[i], norms[j])
            score = float(_trapz(overlap, grid))

            band_scores = {}
            for name, lo, hi in BANDS:
                if lo >= fmax:
                    continue
                m = (grid >= lo) & (grid < min(hi, fmax))
                band_scores[name] = float(_trapz(overlap[m], grid[m])) if m.any() else 0.0
            worst = max(band_scores, key=band_scores.get) if band_scores else None

            # The stem whose dominant frequency is higher gets high-passed so
            # the lower-pitched stem keeps the foundation.
            low_i, high_i = (i, j) if stems[i]["dominant_hz"] <= stems[j]["dominant_hz"] else (j, i)
            dominance = norms[low_i] - norms[high_i]
            dom_mask = dominance > 0.05
            crossover = (float(grid[dom_mask].max()) if dom_mask.any()
                         else min(stems[i]["dominant_hz"], stems[j]["dominant_hz"]))

            conflicts.append({
                "stem_a": stems[i]["name"],
                "stem_b": stems[j]["name"],
                "overlap_score": round(score, 4),
                "most_masked_band": worst,
                "highpass_target": stems[high_i]["name"],
                "recommended_highpass_hz": round(crossover, 1),
            })

    conflicts.sort(key=lambda c: c["overlap_score"], reverse=True)

    warnings = []
    for s in stems:
        if s["peak_dbfs"] > -0.3:
            warnings.append("%s peaks at %.1f dBFS (clipping risk)" % (s["name"], s["peak_dbfs"]))

    return {
        "stem_count": len(stems),
        "stems": stems,
        "conflict_count": len(conflicts),
        "top_conflict": conflicts[0] if conflicts else None,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def _estimate_f0(signal, sr, fmin=20.0, fmax=2000.0):
    """Estimate the fundamental frequency via autocorrelation.

    Autocorrelation is more robust for monophonic pitched material (a bass
    note) than a raw FFT peak, which can latch onto a strong harmonic.
    """
    x = signal - np.mean(signal)
    if np.max(np.abs(x)) < 1e-9:
        return 0.0
    corr = np.correlate(x, x, mode="full")[len(x) - 1:]
    min_lag = max(1, int(sr / fmax))
    max_lag = min(int(sr / fmin), len(corr) - 1)
    if max_lag <= min_lag:
        return 0.0
    lag = min_lag + int(np.argmax(corr[min_lag:max_lag]))
    return sr / lag if lag > 0 else 0.0


def extract_single_cycle(path, out_path, table_size=2048, fmin=20.0, fmax=2000.0):
    """Extract one period of a pitched sample and write it as a single-cycle WAV.

    Detects the fundamental on the loudest ~0.5 s window, extracts exactly one
    period, resamples it to ``table_size`` samples (a power of two is the
    convention for wavetables), normalises it, and writes it out. The result
    can be dragged onto Ableton's Wavetable device to build a custom table —
    the LOM cannot trigger that import, so that final drag is manual.
    """
    sig, sr = load_mono(path)
    if len(sig) == 0:
        raise ValueError("Empty audio file")

    # Pick the loudest ~0.5 s window so we analyse a sustained part, not silence.
    win = min(len(sig), int(sr * 0.5))
    if win < len(sig):
        energy = np.convolve(sig ** 2, np.ones(win), mode="valid")
        start = int(np.argmax(energy))
        segment = sig[start:start + win]
    else:
        segment = sig

    f0 = _estimate_f0(segment, sr, fmin=fmin, fmax=fmax)
    if f0 <= 0:
        raise ValueError("Could not detect a fundamental frequency")

    period = int(round(sr / f0))
    if period < 2:
        raise ValueError("Detected pitch too high to extract a period")

    # Start the cycle at a rising zero crossing for a clean loop point.
    search = segment[: max(period, len(segment) - period)]
    cycle_start = 0
    for k in range(1, len(search)):
        if search[k - 1] <= 0 < search[k]:
            cycle_start = k
            break

    cycle = segment[cycle_start:cycle_start + period]
    if len(cycle) < period:
        cycle = np.pad(cycle, (0, period - len(cycle)))

    # Resample the single period to the target table size (periodic interp).
    src_idx = np.linspace(0, period, num=table_size, endpoint=False)
    table = np.interp(src_idx, np.arange(period + 1),
                      np.concatenate([cycle, cycle[:1]]))

    peak = np.max(np.abs(table))
    if peak > 0:
        table = table / peak

    sf.write(out_path, table.astype(np.float32), sr, subtype="FLOAT")

    return {
        "source": path,
        "out_path": out_path,
        "detected_hz": round(float(f0), 2),
        "detected_period_samples": period,
        "table_size": table_size,
        "sample_rate": sr,
        "note": ("Drag this file onto Ableton's Wavetable device (sample/import "
                 "area) to build a custom wavetable. The Live API cannot trigger "
                 "that import, so this last drag is manual."),
    }
