"""Wavetable-Generator für Abletons Wavetable (Frames à 1024 Samples) und Serum/Vital (--ziel serum: 2048), 32-bit-float-WAV.
Aufruf:  python wavetable_gen.py <modus> [--frames 64] [--ziel ableton|serum] [--name X] [--out ordner] [modus-optionen]
         python wavetable_gen.py alle            -> Starter-Set (jeder Modus einmal)
         python wavetable_gen.py audio --audio bass.wav [--start 0 --end 8]
Modi: morph · pwm · sync · fm · fold · vokal · spektral · zufall · audio
Neben jeder Tabelle entsteht eine Hörprobe (*_hoerprobe.mp3): C2, Position 0 -> 100 % in 4 s.
In Live: WAV aus dem Browser auf die Wellen-Anzeige eines Wavetable-Oszillators ziehen."""
import argparse, os, struct, subprocess
import numpy as np
import soundfile as sf
from scipy.signal import resample

ZIEL = {"ableton": 1024, "serum": 2048}   # Samples pro Frame (Vital liest wie Serum 2048)
N = ZIEL["ableton"]
H = N // 2                    # höchste darstellbare Harmonische
SR = 44100
OUT = os.path.expanduser("~/Music/Ableton/User Library/Samples/Wavetables MCS")


def set_frame(n):
    global N, H
    N, H = n, n // 2


# ---------- Grundbausteine ----------

def from_harmonics(amp, phase=None):
    """Ein Zyklus aus Harmonischen-Amplituden amp[k] (k = 1..len). Sinus-Phase, kein DC."""
    spec = np.zeros(H + 1, complex)
    k = min(len(amp), H)
    ph = np.zeros(k) if phase is None else np.asarray(phase[:k])
    spec[1:k + 1] = np.asarray(amp[:k]) * np.exp(1j * (ph - np.pi / 2)) * N / 2
    return np.fft.irfft(spec, N)


def bandlimit(cycle, oversample=1):
    """Naiv gerechneten Zyklus (Länge N*oversample) auf N Samples bringen, DC und alles über H entfernen."""
    spec = np.fft.rfft(cycle)[:H + 1] / oversample
    spec[0] = 0
    return np.fft.irfft(spec, N)


def naive(fn, oversample=16):
    """fn(phase 0..1) -> Wert; 16-fach überabgetastet und bandbegrenzt (kein Aliasing durch Sprünge)."""
    xo = np.arange(N * oversample) / (N * oversample)
    return bandlimit(fn(xo), oversample)


def finish(frames, rms_match=True, peak_db=-0.3):
    """Frames gleich laut machen (sonst springt die Lautstärke beim Scannen), dann gemeinsam normalisieren."""
    f = np.array(frames, float)
    f -= f.mean(axis=1, keepdims=True)
    if rms_match:
        rms = np.sqrt((f ** 2).mean(axis=1, keepdims=True))
        f /= np.maximum(rms, 1e-9)
    return f * (10 ** (peak_db / 20) / np.abs(f).max())


def ts(n):
    return np.linspace(0, 1, n)


# ---------- Modi (jeder liefert eine Liste von Frames) ----------

def m_morph(n, **_):
    """Sinus -> Dreieck -> Sägezahn -> Rechteck, weich überblendet."""
    k = np.arange(1, H + 1)
    sine = np.r_[1.0, np.zeros(H - 1)]
    tri = np.where(k % 2 == 1, (-1.0) ** ((k - 1) // 2) / k ** 2, 0) * 8 / np.pi ** 2
    saw = 2 / (np.pi * k) * (-1.0) ** (k + 1)
    sq = np.where(k % 2 == 1, 4 / (np.pi * k), 0)
    keys = [sine, tri, saw, sq]
    out = []
    for t in ts(n):
        p = t * 3; i = min(int(p), 2); a = p - i
        out.append(from_harmonics((1 - a) * keys[i] + a * keys[i + 1]))
    return out


def m_pwm(n, min_width=0.04, **_):
    """Rechteck mit Pulsbreite 50 % -> min_width."""
    return [naive(lambda xo, w=w: np.where(xo < w, 1.0, -1.0)) for w in np.linspace(0.5, min_width, n)]


def m_sync(n, ratio=6.5, **_):
    """Hard-Sync: Slave-Sägezahn mit Verhältnis 1 -> ratio (der klassische Sync-Sweep)."""
    return [naive(lambda xo, r=r: 2 * ((xo * r) % 1.0) - 1) for r in np.linspace(1, ratio, n)]


def m_fm(n, ratio=2.0, index=6.0, **_):
    """2-Operator-FM: Träger-Sinus, Modulator ratio, Index 0 -> index."""
    return [naive(lambda xo, i=i: np.sin(2 * np.pi * xo + i * np.sin(2 * np.pi * ratio * xo))) for i in np.linspace(0, index, n)]


def m_fold(n, drive=8.0, **_):
    """Wavefolder auf Sinus: Verstärkung 1 -> drive, gefaltet statt geclippt (Westcoast-Obertöne)."""
    def fold(v):
        return 4 * np.abs(((v - 1) / 4) % 1 - 0.5) - 1
    return [naive(lambda xo, g=g: fold(g * np.sin(2 * np.pi * xo))) for g in np.linspace(1, drive, n)]


VOKALE = {  # Formanten F1, F2, F3 in Hz (Männerstimme, gerundet)
    "a": (730, 1090, 2440), "e": (530, 1840, 2480), "i": (270, 2290, 3010),
    "o": (570, 840, 2410), "u": (300, 870, 2240)}


def m_vokal(n, folge="aeiou", f0=65.4, **_):
    """Sägezahn-Spektrum durch wandernde Vokal-Formanten (für einen Grundton um f0, Standard C2)."""
    k = np.arange(1, H + 1); hz = k * f0
    base = 1 / k
    pts = [VOKALE[c] for c in folge]
    out = []
    for t in ts(n):
        p = t * (len(pts) - 1); i = min(int(p), len(pts) - 2); a = p - i
        fm = [(1 - a) * pts[i][j] + a * pts[i + 1][j] for j in range(3)]
        env = sum(g * np.exp(-0.5 * ((hz - f) / (f * 0.12)) ** 2) for f, g in zip(fm, (1.0, 0.7, 0.4)))
        out.append(from_harmonics(base * (0.08 + env)))
    return out


def m_spektral(n, breite=3.0, bis=64, **_):
    """Resonanz-Spitze wandert durch die Harmonischen 1 -> bis (wie ein Filter-Sweep, aber in der Welle)."""
    k = np.arange(1, H + 1); base = 1 / k
    out = []
    for c in np.geomspace(1, bis, n):
        out.append(from_harmonics(base * (0.15 + 4 * np.exp(-0.5 * ((k - c) / breite) ** 2))))
    return out


def m_zufall(n, seed=129, harm=48, **_):
    """Zufällige Spektren, weich überblendet (Seed = reproduzierbar). Gut für Pads und Texturen."""
    rng = np.random.default_rng(seed)
    keys = [rng.random(harm) / np.arange(1, harm + 1) ** 0.7 for _ in range(5)]
    out = []
    for t in ts(n):
        p = t * 4; i = min(int(p), 3); a = p - i
        a = 0.5 - 0.5 * np.cos(np.pi * a)
        out.append(from_harmonics((1 - a) * keys[i] + a * keys[i + 1]))
    return out


def _period(sig, sr, fmin=30, fmax=1000):
    """Periodenlänge in Samples per Autokorrelation (None, wenn kein klarer Ton)."""
    s = sig - sig.mean()
    ac = np.fft.irfft(np.abs(np.fft.rfft(s, 2 * len(s))) ** 2)[:len(s)]
    lo, hi = int(sr / fmax), min(int(sr / fmin), len(s) - 1)
    if hi <= lo or ac[0] <= 0:
        return None
    p = lo + int(np.argmax(ac[lo:hi]))
    return p if ac[p] / ac[0] > 0.3 else None


def m_audio(n, audio=None, start=0.0, end=None, **_):
    """Audio -> Wavetable: an n Stellen je eine Periode ausschneiden und auf die Frame-Größe bringen.
    Funktioniert bei tonalen Klängen (Bass, Synth, Stimme); Stellen ohne klaren Ton werden übersprungen."""
    sig, sr = sf.read(audio, always_2d=True)
    sig = sig.mean(axis=1)
    a = int(start * sr); b = len(sig) if end is None else int(end * sr)
    sig = sig[a:b]
    win = int(sr * 0.08)
    out = []
    for pos in np.linspace(0, len(sig) - 2 * win, n * 3).astype(int):
        seg = sig[pos:pos + 2 * win]
        p = _period(seg, sr)
        if p is None or np.sqrt((seg ** 2).mean()) < 1e-3:
            continue
        cyc = seg[win:win + p]
        out.append(bandlimit(resample(cyc, N * 4), 4))
        if len(out) == n:
            break
    if len(out) < 2:
        raise SystemExit("Kein klarer Ton gefunden – Ausschnitt mit gehaltener Note wählen (--start/--end).")
    return out


MODI = {"morph": m_morph, "pwm": m_pwm, "sync": m_sync, "fm": m_fm, "fold": m_fold,
        "vokal": m_vokal, "spektral": m_spektral, "zufall": m_zufall, "audio": m_audio}


# ---------- Ausgabe ----------

def write_wav(path, frames):
    """32-bit-float-WAV mit 'clm '-Chunk (Serum/Vital erkennen daran die Frame-Größe; Live ignoriert ihn)."""
    data = frames.astype("<f4").ravel().tobytes()
    clm = f"<!>{N} 00000000 wavetable (MCS)".encode().ljust(48, b" ")
    fmt = struct.pack("<HHIIHH", 3, 1, SR, SR * 4, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"clm " + struct.pack("<I", len(clm)) + clm
    body += b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def preview(frames, path, f0=65.41, seconds=4.0):
    """Hörprobe: Ton f0 (C2), Wavetable-Position 0 -> 100 %. Pro Frame auf Nyquist begrenzt, wie im Synth."""
    lim = int(SR / 2 / f0)
    bl = np.fft.irfft(np.fft.rfft(frames, axis=1)[:, :lim + 1], N, axis=1)
    n = int(SR * seconds)
    ph = (np.arange(n) * f0 / SR) % 1.0 * N
    pos = np.linspace(0, len(bl) - 1, n)
    i0 = np.minimum(pos.astype(int), len(bl) - 2); a = pos - i0
    j0 = ph.astype(int); j1 = (j0 + 1) % N; b = ph - j0
    s0 = bl[i0, j0] * (1 - b) + bl[i0, j1] * b
    s1 = bl[i0 + 1, j0] * (1 - b) + bl[i0 + 1, j1] * b
    y = (1 - a) * s0 + a * s1
    fade = np.minimum(1, np.minimum(np.arange(n), n - np.arange(n)) / (0.01 * SR))
    y = y * fade * 10 ** (-6 / 20) / max(1e-9, np.abs(y).max())
    tmp = path + ".tmp.wav"
    sf.write(tmp, y, SR, subtype="PCM_16")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-b:a", "192k", path], check=True)
    os.remove(tmp)


def make(modus, frames=64, name=None, out=OUT, hoerprobe=True, rms_match=True, ziel="ableton", **opt):
    set_frame(ZIEL[ziel])
    os.makedirs(out, exist_ok=True)
    f = finish(MODI[modus](frames, **opt), rms_match=rms_match)
    if name is None:
        extra = "_".join(f"{k}{v}" for k, v in opt.items() if v is not None and k != "audio")
        src = os.path.splitext(os.path.basename(opt["audio"]))[0][:40] if opt.get("audio") else ""
        name = "_".join(s for s in ["MCS", modus, src, extra, "" if ziel == "ableton" else ziel] if s)
    wav = os.path.join(out, name + ".wav")
    write_wav(wav, f)
    if hoerprobe:
        preview(f, os.path.join(out, name + "_hoerprobe.mp3"))
    return wav, len(f)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("modus", choices=list(MODI) + ["alle"])
    ap.add_argument("--frames", type=int, default=64)
    ap.add_argument("--ziel", choices=list(ZIEL), default="ableton")
    ap.add_argument("--name"); ap.add_argument("--out", default=OUT)
    ap.add_argument("--keine-hoerprobe", action="store_true")
    ap.add_argument("--ohne-rms", action="store_true", help="Lautstärke-Unterschiede der Frames behalten")
    for k, t in [("ratio", float), ("index", float), ("drive", float), ("min_width", float), ("folge", str),
                 ("breite", float), ("bis", int), ("seed", int), ("audio", str), ("start", float), ("end", float)]:
        ap.add_argument("--" + k.replace("_", "-"), dest=k, type=t)
    a = ap.parse_args()
    opt = {k: v for k, v in vars(a).items() if k not in ("modus", "frames", "name", "out", "keine_hoerprobe", "ohne_rms", "ziel") and v is not None}
    for m in ([m for m in MODI if m != "audio"] if a.modus == "alle" else [a.modus]):
        wav, nf = make(m, a.frames, None if a.modus == "alle" else a.name, a.out, not a.keine_hoerprobe, not a.ohne_rms, a.ziel, **(opt if m == a.modus else {}))
        print(f"{nf:3d} Frames  {wav}")
