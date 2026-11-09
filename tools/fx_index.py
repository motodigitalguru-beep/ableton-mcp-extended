"""FX-Library katalogisieren: Länge (Takte bei BPM), Tonart (Schätzung), Lautheit, Spitze, Typ.
Schreibt eine Markdown-Übersicht (z. B. in den Obsidian-Vault) und schlägt Namen nach dem Schema
<Typ>_<Länge>T_<BPM>_<Ton>_<Quelle>.wav für unsortierte Dateien vor (--umbenennen benennt wirklich um).
Aufruf: python fx_index.py <fx-ordner> <ausgabe.md> [bpm] [--umbenennen]"""
import os, re, sys, glob
import numpy as np
import soundfile as sf

NN = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
KK_MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
TYPES = ("Riser", "Downlifter", "Impact", "Sweep", "Texture", "Transition", "Vocal FX")
PATTERN = re.compile(r"^(%s)_(\d+(?:\.\d+)?T|OneShot)_(\d+)_([A-G]#?|x)_(.+)\.wav$" % "|".join(TYPES))


def key_estimate(m, sr):
    """Tonart über Chroma (Spektrum -> Tonklassen) + Krumhansl-Profil; None wenn atonal."""
    n = min(len(m), sr * 8)
    S = np.abs(np.fft.rfft(m[:n] * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / sr)
    sel = (f > 50) & (f < 2000)
    pc = np.mod(np.round(12 * np.log2(f[sel] / 440.0) + 9), 12).astype(int)
    chroma = np.bincount(pc, weights=S[sel], minlength=12)
    chroma = chroma / (chroma.sum() + 1e-20)
    flat = chroma.max() / (chroma.mean() + 1e-20)
    if flat < 2.2:            # zu gleichmäßig verteilt = Rauschen/atonal
        return None, round(float(flat), 1)
    best = max(((np.corrcoef(chroma, np.roll(p, r))[0, 1], NN[r] + (" Moll" if p is KK_MIN else ""))
                for r in range(12) for p in (KK_MAJ, KK_MIN)))
    return best[1], round(float(flat), 1)


def guess_type(m, sr):
    """Grobe Typ-Schätzung über den Lautheitsverlauf (Anstieg/Abfall/Impuls/flach)."""
    blk = sr // 20
    e = np.sqrt(np.mean(m[:len(m) // blk * blk].reshape(-1, blk) ** 2, 1)) + 1e-9
    db = 20 * np.log10(e / e.max())
    first, last = db[: max(1, len(db) // 5)].mean(), db[-max(1, len(db) // 5):].mean()
    peak_pos = np.argmax(db) / len(db)
    if peak_pos < 0.1 and last < first - 15:
        return "Impact"
    if last > first + 10:
        return "Riser"
    if first > last + 10:
        return "Downlifter"
    return "Texture"


def analyse(path, bpm):
    x, sr = sf.read(path, always_2d=True)
    m = x.mean(1)
    dur = len(m) / sr
    bars = dur / (4 * 60 / bpm)
    try:
        import pyloudnorm as pyln
        lufs = round(float(pyln.Meter(sr).integrated_loudness(x)), 1)
    except Exception:
        lufs = None
    h = len(m) // 2
    k1, t1 = key_estimate(m[:h], sr); k2, t2 = key_estimate(m[h:], sr); key, tonal = key_estimate(m, sr)
    if not (k1 and k2 and k1 == k2 and key == k1 and min(t1, t2) >= 3.0):
        key = None   # gleitend (Sweeps) oder atonal (Rauschen) -> keine Tonart behaupten
    return {"datei": os.path.basename(path), "ordner": os.path.basename(os.path.dirname(path)),
            "sek": round(dur, 2), "takte": round(bars, 2), "tonart": key or "gleitend/atonal", "tonal": tonal,
            "lufs": lufs, "peak": round(float(20 * np.log10(np.abs(x).max() + 1e-12)), 1),
            "typ_schaetzung": guess_type(m, sr), "schema_ok": bool(PATTERN.match(os.path.basename(path)))}


def suggest_name(r, bpm):
    typ = r["ordner"] if r["ordner"] in TYPES else r["typ_schaetzung"]
    ln = "OneShot" if abs(r["takte"] - round(r["takte"])) > 0.05 or r["takte"] < 0.95 else "%dT" % round(r["takte"])
    ton = r["tonart"].split()[0] if r["tonart"] != "gleitend/atonal" else "x"
    src = re.sub(r"[^A-Za-z0-9äöüÄÖÜß-]+", "-", os.path.splitext(r["datei"])[0]).strip("-")[:30] or "Quelle"
    return "%s_%s_%d_%s_%s.wav" % (typ, ln, bpm, ton, src)


def main():
    folder, out = sys.argv[1], sys.argv[2]
    bpm = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 129
    rename = "--umbenennen" in sys.argv
    rows = [analyse(p, bpm) for p in sorted(glob.glob(os.path.join(folder, "**", "*.wav"), recursive=True))]
    lines = ["---", "typ: library", "status: gesichert", "quelle: automatisch (fx_index.py)", "---", "", "# FX-Library", "", "Automatisch erzeugt mit `tools/fx_index.py` (BPM %d)." % bpm, "",
             "| Ordner | Datei | Takte | Tonart | LUFS | Spitze | Schema |", "|---|---|---|---|---|---|---|"]
    renamed = []
    for r in rows:
        name = r["datei"]
        if not r["schema_ok"]:
            new = suggest_name(r, bpm)
            if rename:
                src = os.path.join(folder, r["ordner"], r["datei"])
                dst = os.path.join(folder, r["ordner"] if r["ordner"] in TYPES else new.split("_")[0], new)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if not os.path.exists(dst):
                    os.rename(src, dst); renamed.append((r["datei"], new)); name = new
            else:
                name += " → `%s`" % new
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["ordner"], name, r["takte"], r["tonart"], r["lufs"], r["peak"], "✔" if r["schema_ok"] else "✗"))
    lines += ["", "Schema: `<Typ>_<Länge>T_<BPM>_<Ton>_<Quelle>.wav` · Typen: " + ", ".join(TYPES), ""]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write("\n".join(lines))
    print("%d Dateien katalogisiert -> %s" % (len(rows), out))
    for a, b in renamed:
        print("umbenannt:", a, "->", b)


if __name__ == "__main__":
    main()
