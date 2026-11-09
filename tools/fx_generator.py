"""FX-Starterpaket synthetisch erzeugen: taktgenau (BPM), auf Grundton gestimmt, 24-bit-WAV.
Aufruf: python fx_generator.py <zielordner> [bpm] [grundton z.B. D]
Benennung: <Typ>_<Länge>T_<BPM>_<Ton>_<Quelle>.wav  (T = Takte)"""
import os, sys
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, fftconvolve

SR = 44100
NN = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
rng = np.random.default_rng(129)


def note_hz(name, octave):
    return 440.0 * 2 ** ((NN.index(name) + 12 * (octave + 1) - 69) / 12)


def bars(n, bpm):
    return int(round(n * 4 * 60 / bpm * SR))


def stereo_noise(n, width=1.0):
    a, b = rng.standard_normal(n), rng.standard_normal(n)
    mid = (a + b) / 2
    return np.stack([mid + width * (a - mid), mid + width * (b - mid)], 1)


def _svf(x, fc, q, mode):
    """Zustandsvariablen-Filter (TPT/Zavalishin), Grenzfrequenz pro Sample -> klickfreie Sweeps.
    x: (n, ch); fc: (n,) Hz; mode: 'bp' oder 'lp'."""
    g = np.tan(np.pi * np.clip(fc, 20, SR * 0.45) / SR)
    k = 1.0 / q
    a1 = 1 / (1 + g * (g + k)); a2 = g * a1; a3 = g * a2
    out = np.zeros_like(x)
    for c in range(x.shape[1]):
        ic1 = ic2 = 0.0
        xc = x[:, c]; oc = out[:, c]
        for i in range(len(xc)):
            v3 = xc[i] - ic2
            v1 = a1[i] * ic1 + a2[i] * v3
            v2 = ic2 + a2[i] * ic1 + a3[i] * v3
            ic1 = 2 * v1 - ic1; ic2 = 2 * v2 - ic2
            oc[i] = v1 if mode == "bp" else v2
    return out


def _path(n, f0, f1):
    return f0 * (f1 / f0) ** (np.arange(n) / max(1, n - 1))


def sweep_bp(x, f0, f1, q=1.2, steps=None):
    return _svf(x, _path(len(x), f0, f1), q, "bp") * np.sqrt(q)


def lp_sweep(x, f0, f1, steps=None):
    return _svf(x, _path(len(x), f0, f1), 0.707, "lp")


def supersaw(f_start, f_end, n, voices=7, detune=0.012):
    t = np.arange(n) / SR
    f = f_start * (f_end / f_start) ** (t / t[-1])
    out = np.zeros((n, 2))
    for v in range(voices):
        d = 1 + detune * (v - (voices - 1) / 2) / ((voices - 1) / 2)
        ph = np.cumsum(f * d) / SR + rng.random()
        saw = 2 * (ph % 1.0) - 1
        pan = v / (voices - 1)
        out[:, 0] += saw * (1 - pan); out[:, 1] += saw * pan
    return out / voices


def reverb_ir(seconds=2.5, decay=3.0):
    n = int(seconds * SR); t = np.arange(n) / SR
    ir = stereo_noise(n) * np.exp(-decay * t)[:, None]
    ir = sosfilt(butter(2, 6000, "lowpass", fs=SR, output="sos"), ir, axis=0)
    return ir / np.abs(ir).max()


def norm(x, peak_db=-1.0, fade_in=0.002, fade_out=0.02):
    x = x.copy()
    fi, fo = int(fade_in * SR), int(fade_out * SR)
    if fi: x[:fi] *= np.linspace(0, 1, fi)[:, None]
    if fo: x[-fo:] *= np.linspace(1, 0, fo)[:, None]
    return x / (np.abs(x).max() + 1e-12) * 10 ** (peak_db / 20)


def ramp_db(n, db0, db1, curve=2.0):
    t = np.linspace(0, 1, n) ** curve
    return (10 ** ((db0 + (db1 - db0) * t) / 20))[:, None]


def make_all(out, bpm=129, key="D"):
    made = []
    def save(kind, nbars, src, x, tonal=True):
        d = os.path.join(out, kind); os.makedirs(d, exist_ok=True)
        L = ("%gT" % nbars) if nbars else "OneShot"
        fn = "%s_%s_%d_%s_%s.wav" % (kind, L, bpm, key if tonal else "x", src)
        sf.write(os.path.join(d, fn), norm(x), SR, subtype="PCM_24"); made.append(os.path.join(kind, fn))

    # --- Riser ---
    for nb, q, src in ((8, 1.2, "Rauschen"), (4, 3.5, "Rauschen-Resonanz")):
        n = bars(nb, bpm); x = stereo_noise(n, width=1.0)
        x = sweep_bp(x, 200, 12000, q=q) * ramp_db(n, -30, 0, 2.2)
        save("Riser", nb, src, x, tonal=False)
    n = bars(4, bpm)
    x = supersaw(note_hz(key, 3), note_hz(key, 5), n)
    x = lp_sweep(x, 300, 14000) * ramp_db(n, -24, 0, 1.6)
    save("Riser", 4, "Supersaw-Pitch", x)
    # Snare-Roll aus Rauschimpulsen: 1/4 -> 1/8 -> 1/16 -> 1/32 über 4 Takte
    n = bars(4, bpm); x = np.zeros((n, 2)); beat = 60 / bpm
    hits = []
    for bar, div in enumerate((1, 2, 4, 8)):
        for k in range(4 * div):
            hits.append((bar * 4 + k / div) * beat)
    for i, t0 in enumerate(hits):
        a = int(t0 * SR); m = int(0.12 * SR)
        if a + m > n: m = n - a
        env = np.exp(-np.arange(m) / (0.035 * SR))[:, None]
        burst = stereo_noise(m, 0.6) * env
        burst = sosfilt(butter(2, [200 + 4000 * i / len(hits), 9000], "bandpass", fs=SR, output="sos"), burst, axis=0)
        body = np.sin(2 * np.pi * note_hz(key, 3) * np.arange(m) / SR)[:, None] * env * 0.5
        x[a:a + m] += (burst + body) * (0.25 + 0.75 * i / len(hits))
    save("Riser", 4, "Snare-Roll", x)

    # --- Downlifter ---
    n = bars(2, bpm); x = stereo_noise(n); x = sweep_bp(x, 10000, 150, q=1.2) * ramp_db(n, 0, -36, 0.6)
    save("Downlifter", 2, "Rauschen", x, tonal=False)
    x = supersaw(note_hz(key, 5), note_hz(key, 2), n)
    x = lp_sweep(x, 12000, 200) * ramp_db(n, 0, -30, 0.7)
    save("Downlifter", 2, "Supersaw-Pitch", x)

    # --- Impacts ---
    m = int(2.5 * SR); t = np.arange(m) / SR
    f = note_hz(key, 2) * (note_hz(key, 0) / note_hz(key, 2)) ** np.minimum(1, t / 1.2)
    sub = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.6)
    sub = np.tanh(sub * 2.2) / np.tanh(2.2)
    click = sosfilt(butter(2, 2000, "highpass", fs=SR, output="sos"), rng.standard_normal(m)) * np.exp(-t * 80) * 0.4
    save("Impact", 0, "Sub-Drop", np.stack([sub + click, sub + click], 1))
    boom = stereo_noise(m) * np.exp(-t * 6)[:, None]
    boom = sosfilt(butter(2, 900, "lowpass", fs=SR, output="sos"), boom, axis=0)
    low = (np.sin(2 * np.pi * note_hz(key, 1) * t) * np.exp(-t * 2.5))[:, None]
    x = boom * 0.6 + low
    x = x + 0.35 * fftconvolve(x, reverb_ir(2.5, 2.2), axes=0)[:m]
    save("Impact", 0, "Boom", x)
    # metallischer Crash: inharmonische Partials + Rauschen
    partials = [note_hz(key, 5) * r for r in (1, 1.47, 2.09, 2.56, 3.13, 3.89, 4.42)]
    crash = sum(np.sin(2 * np.pi * p * t + rng.random() * 6) for p in partials)[:, None] * 0.15
    crash = crash + sosfilt(butter(2, 3000, "highpass", fs=SR, output="sos"), stereo_noise(m), axis=0) * 0.6
    crash = crash * np.exp(-t * 1.8)[:, None]
    save("Impact", 0, "Crash-Metall", crash)

    # --- Sweeps ---
    n = bars(1, bpm)
    save("Sweep", 1, "Rauschen-hoch", sweep_bp(stereo_noise(n), 400, 14000, q=2.5) * ramp_db(n, -18, 0, 1.0), tonal=False)
    save("Sweep", 1, "Rauschen-runter", sweep_bp(stereo_noise(n), 14000, 400, q=2.5) * ramp_db(n, 0, -18, 1.0), tonal=False)
    n = bars(2, bpm); x = stereo_noise(n)
    half = n // 2
    path = np.concatenate([_path(half, 300, 9000), _path(n - half, 9000, 300)])
    whoosh = _svf(x, path, 1.8, "bp") * np.sqrt(1.8)
    env = np.concatenate([np.linspace(0.05, 1, half) ** 2, np.linspace(1, 0.02, n - half) ** 1.5])[:, None]
    save("Transition", 2, "Whoosh", whoosh * env, tonal=False)

    # --- Transition: Reverse-Crash (Swell) ---
    rev = crash[::-1][-bars(1, bpm):]
    save("Transition", 1, "Reverse-Crash", rev)

    # --- Texturen ---
    n = bars(4, bpm); t = np.arange(n) / SR
    air = sosfilt(butter(2, [3000, 12000], "bandpass", fs=SR, output="sos"), stereo_noise(n), axis=0)
    air *= (0.6 + 0.4 * np.sin(2 * np.pi * t / (4 * 60 / bpm)))[:, None]
    save("Texture", 4, "Luft", air, tonal=False)
    # Drone NUR über 400 Hz (Lehre vom Pad: Bass-Obertöne frei lassen)
    tones = [note_hz(key, 4), note_hz("A", 4), note_hz(key, 5)]
    dr = sum(np.sin(2 * np.pi * f * t + 0.3 * np.sin(2 * np.pi * 0.2 * t + i)) for i, f in enumerate(tones))
    dr = np.stack([dr, np.roll(dr, int(0.011 * SR))], 1)
    save("Texture", 4, "Drone-hoch", dr * ramp_db(n, -12, 0, 0.5))
    return made


if __name__ == "__main__":
    out = sys.argv[1]
    bpm = float(sys.argv[2]) if len(sys.argv) > 2 else 129
    key = sys.argv[3] if len(sys.argv) > 3 else "D"
    files = make_all(out, int(bpm), key)
    print(len(files), "FX erzeugt"); print("\n".join(files))
