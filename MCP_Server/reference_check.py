"""Referenz-Check: eigener Render gegen Profi-Referenzen (Ampel je Kriterium).

Kriterien stammen aus der Favella-Arbeit (16.10.2026) gegen Comusa/Ramp:
Lautheit, Bass-Gewicht, Brummen vor der nächsten Kick, Kick-Punch, Low-Mids,
Mitten, Höhen, Dichte (PLR), True Peak, Stereo im Bass.
Verglichen wird jeweils der lauteste 30-s-Abschnitt (= Drop) jeder Datei.
"""
from __future__ import annotations

import numpy as np
import soundfile as sf
import pyloudnorm as pyln
from scipy.signal import butter, sosfiltfilt, welch, find_peaks, resample_poly

BANDS = {"sub_30_60": (30, 60), "kick_60_120": (60, 120), "lowmid_120_300": (120, 300),
         "mid_300_2k": (300, 2000), "presence_2k_6k": (2000, 6000), "air_6k_16k": (6000, 16000)}

# Toleranz je Kriterium (dB): grün <= tol, gelb <= 2*tol, sonst rot.
TOL = {"lufs_drop": 1.0, "bass_rel": 1.0, "hum_before_kick": 2.0, "kick_body": 1.5, "plr": 1.5,
       "sub_30_60": 1.5, "kick_60_120": 1.5, "lowmid_120_300": 1.5, "mid_300_2k": 1.5,
       "presence_2k_6k": 1.5, "air_6k_16k": 1.5, "bass_side": 6.0}

LABEL = {"lufs_drop": "Lautheit Drop (LUFS)", "bass_rel": "Bass <150 Hz rel. Lautheit",
         "hum_before_kick": "Tiefbass vor nächster Kick (Brummen)", "kick_body": "Kick ragt heraus",
         "plr": "Dichte (PLR, klein = dichter)", "sub_30_60": "Sub 30–60", "kick_60_120": "Kick 60–120",
         "lowmid_120_300": "Low-Mids 120–300", "mid_300_2k": "Mitten 300–2k", "presence_2k_6k": "Präsenz 2–6k",
         "air_6k_16k": "Höhen 6–16k", "bass_side": "Stereo im Bass (Side/Mid <120 Hz)"}


def _read(path: str):
    x, sr = sf.read(path, always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x, sr


def _loudest(x, sr, sec=30):
    mt = pyln.Meter(sr)
    if len(x) <= sec * sr:
        return x
    best = None
    for i in range(0, len(x) - sec * sr, 5 * sr):
        l = mt.integrated_loudness(x[i:i + sec * sr])
        if best is None or l > best[0]:
            best = (l, i)
    return x[best[1]:best[1] + sec * sr]


def analyze(path: str, section_seconds: float = 30.0) -> dict:
    x, sr = _read(path)
    d = _loudest(x, sr, int(section_seconds))
    m = d.mean(axis=1)
    L = pyln.Meter(sr).integrated_loudness(d)
    tp = 20 * np.log10(np.abs(resample_poly(d, 4, 1, axis=0)).max() + 1e-12)
    f, P = welch(m, sr, nperseg=16384)
    tot = P.sum()
    out = {"lufs_drop": L, "true_peak": tp, "plr": tp - L}
    for k, (a, b) in BANDS.items():
        out[k] = 10 * np.log10(P[(f >= a) & (f < b)].sum() / tot + 1e-20)
    lo = sosfiltfilt(butter(4, 150, "lp", fs=sr, output="sos"), m)
    out["bass_rel"] = 20 * np.log10(np.sqrt(np.mean(lo ** 2)) + 1e-12) - L
    # Brummen: Tiefbass 5–30 ms vor jedem Kick-Einsatz relativ zum Kick-Peak
    b = sosfiltfilt(butter(4, [35, 70], "bp", fs=sr, output="sos"), m)
    w = int(0.01 * sr)
    e = np.sqrt(np.convolve(b ** 2, np.ones(w) / w, "same"))
    dd = np.diff(e)
    on, _ = find_peaks(dd, distance=int(0.35 * sr), height=dd.max() * 0.2)
    on = [o for o in on if o > int(0.05 * sr) and o + int(0.1 * sr) < len(e)]
    if len(on) > 3:
        pre = [e[o - int(0.03 * sr):o - int(0.005 * sr)].mean() for o in on[1:-1]]
        pk = [e[o:o + int(0.1 * sr)].max() for o in on[1:-1]]
        out["hum_before_kick"] = 20 * np.log10(np.median(pre) / np.median(pk) + 1e-12)
    # Kick ragt heraus: 95. zu 50. Perzentil der 40-ms-Pegel
    w2 = int(0.04 * sr)
    fr = np.array([np.sqrt(np.mean(m[i:i + w2] ** 2)) for i in range(0, len(m) - w2, w2)])
    out["kick_body"] = 20 * np.log10(np.percentile(fr, 95) / (np.percentile(fr, 50) + 1e-12))
    sos = butter(4, 120, "lp", fs=sr, output="sos")
    Ls, Rs = sosfiltfilt(sos, d[:, 0]), sosfiltfilt(sos, d[:, 1])
    out["bass_side"] = 20 * np.log10(np.std(Ls - Rs) / (np.std(Ls + Rs) + 1e-12) + 1e-12)
    return {k: round(float(v), 1) for k, v in out.items()}


def compare(target: str, references: list[str]) -> dict:
    t = analyze(target)
    refs = {r: analyze(r) for r in references}
    rows = []
    for k in LABEL:
        if k not in t:
            continue
        vals = [v[k] for v in refs.values() if k in v]
        if not vals:
            continue
        ref = float(np.mean(vals))
        diff = t[k] - ref
        tol = TOL.get(k, 1.5)
        light = "🟢" if abs(diff) <= tol else ("🟡" if abs(diff) <= 2 * tol else "🔴")
        if k == "bass_side":   # weniger Seite ist nie schlecht
            light = "🟢" if t[k] <= ref + tol else ("🟡" if t[k] <= ref + 2 * tol else "🔴")
        rows.append({"kriterium": LABEL[k], "key": k, "track": t[k], "referenz": round(ref, 1),
                     "diff": round(diff, 1), "ampel": light})
    tp_ok = t.get("true_peak", 0) <= -1.0
    return {"target": target, "references": references, "rows": rows,
            "true_peak": t.get("true_peak"), "true_peak_ok": tp_ok, "target_raw": t, "refs_raw": refs}


def as_table(res: dict) -> str:
    lines = ["| Kriterium | Track | Referenz | Δ | |", "|---|---|---|---|---|"]
    for r in res["rows"]:
        lines.append("| %s | %.1f | %.1f | %+.1f | %s |" % (r["kriterium"], r["track"], r["referenz"], r["diff"], r["ampel"]))
    lines.append("| True Peak | %.1f dBTP | ≤ −1,0 | | %s |" % (res["true_peak"], "🟢" if res["true_peak_ok"] else "🔴"))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    r = compare(sys.argv[1], sys.argv[2:])
    print(as_table(r))
