"""Baut die Max-for-Live-Geräte 'LEK Bass' und 'LEK Kick' (.amxd) – Low-End-Kopplung.
Aufruf: python build_lek.py <zielordner>"""
import json, struct, sys, os, shutil

BASS_GEN = r'''
// LEK Bass: duckt nur den Tiefbass (unter xover) im Kick-Raster, mit Lookahead.
Buffer kick("lek_kickmap");
Param ticks(0); Param tempo(120); Param playing(0);
Param nkick(0); Param rescan(0);
Param xover(120); Param depth(1); Param hold(0.01); Param relpct(1); Param pre(0.003);
Param autorel(1); Param relman(0.15); Param kicklen(0.2);
History bc(0); History lastph(0); History idx(0); History last(-1000000); History lastreset(-1);
History gs(1);
History ax1(0); History ax2(0); History ay1(0); History ay2(0);
History bx1(0); History bx2(0); History by1(0); History by2(0);
History cx1(0); History cx2(0); History cy1(0); History cy2(0);
History dx1(0); History dx2(0); History dy1(0); History dy2(0);

ph = in3;
seek = 0;
if (ph < lastph - 0.5) { bc = bc + 1; }
lastph = ph;
pos = bc + ph;
if (playing > 0.5 && abs(pos - ticks) > 0.6) { bc = floor(ticks - ph + 0.5); pos = bc + ph; seek = 1; }
if (rescan != lastreset) { lastreset = rescan; seek = 1; }
prebeats = pre * tempo / 60;
look = pos + prebeats;
if (seek) { idx = 0; last = -1000000; }
for (i = 0; i < 4096; i += 1) {
    if (idx >= nkick) { break; }
    k = peek(kick, idx);
    if (k > look) { break; }
    last = k; idx = idx + 1;
}
rel = relman;
if (autorel > 0.5) { rel = max(0.02, kicklen * relpct - hold); }
t = (look - last) * 60 / tempo;
s = 0;
if (playing > 0.5 && t >= 0) {
    if (t < pre) { s = t / max(pre, 0.0001); }
    else if (t < pre + hold) { s = 1; }
    else if (t < pre + hold + rel) { x = (t - pre - hold) / rel; s = 0.5 * (1 + cos(pi * x)); }
}
g = 1 - depth * s;
gs = gs + (g - gs) * 0.02;

// Linkwitz-Riley-Tiefpass (2x Butterworth 2. Ordnung) je Kanal
w0 = 2 * pi * clamp(xover, 30, 400) / samplerate;
cw = cos(w0); al = sin(w0) / (2 * 0.70710678);
a0 = 1 + al;
b0 = (1 - cw) / 2 / a0; b1 = (1 - cw) / a0; b2 = b0;
a1 = -2 * cw / a0; a2 = (1 - al) / a0;

l1 = b0 * in1 + b1 * ax1 + b2 * ax2 - a1 * ay1 - a2 * ay2; ax2 = ax1; ax1 = in1; ay2 = ay1; ay1 = l1;
l2 = b0 * l1 + b1 * bx1 + b2 * bx2 - a1 * by1 - a2 * by2; bx2 = bx1; bx1 = l1; by2 = by1; by1 = l2;
r1 = b0 * in2 + b1 * cx1 + b2 * cx2 - a1 * cy1 - a2 * cy2; cx2 = cx1; cx1 = in2; cy2 = cy1; cy1 = r1;
r2 = b0 * r1 + b1 * dx1 + b2 * dx2 - a1 * dy1 - a2 * dy2; dx2 = dx1; dx1 = r1; dy2 = dy1; dy1 = r2;

out1 = in1 - (1 - gs) * l2;
out2 = in2 - (1 - gs) * r2;
out3 = 1 - gs;
out4 = pos; out5 = nkick; out6 = last; out7 = playing; out8 = kicklen;
'''

KICK_GEN = r'''
// LEK Kick: misst die Länge des Kick-Ausklangs (bis -12 dB unter Spitze, Tiefband).
Param thresh(0.03);
History x1(0); History x2(0); History y1(0); History y2(0);
History env(0); History state(0); History cnt(0); History pk(0); History len(0.2); History hits(0); History quiet(1);
m = (in1 + in2) * 0.5;
w0 = 2 * pi * 150 / samplerate; cw = cos(w0); al = sin(w0) / (2 * 0.70710678); a0 = 1 + al;
b0 = (1 - cw) / 2 / a0; b1 = (1 - cw) / a0; b2 = b0; a1 = -2 * cw / a0; a2 = (1 - al) / a0;
lp = b0 * m + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2; x2 = x1; x1 = m; y2 = y1; y1 = lp;
a = abs(lp);
rc = exp(-1 / (0.008 * samplerate));
env = a > env ? a : env * rc;
if (state < 0.5) {
    if (env > thresh && quiet > 0.5) { state = 1; cnt = 0; pk = env; quiet = 0; }
} else {
    cnt = cnt + 1;
    if (env > pk) { pk = env; }
    if (env < pk * 0.25 && cnt > 0.01 * samplerate) { len = cnt / samplerate; hits = hits + 1; state = 0; }
    if (cnt > 2 * samplerate) { state = 0; }
}
if (env < thresh * 0.5) { quiet = 1; }
out1 = in1; out2 = in2; out3 = len; out4 = hits;
'''

APPV = {"major": 8, "minor": 1, "revision": 2, "architecture": "x64", "modernui": 1}
_id = [0]
def nid():
    _id[0] += 1
    return "obj-%d" % _id[0]

def box(objtext, x, y, w=80, h=22, inl=1, outl=1, otype=None, maxclass="newobj", **extra):
    b = {"id": nid(), "maxclass": maxclass, "numinlets": inl, "numoutlets": outl,
         "patching_rect": [x, y, w, h], "fontname": "Arial Bold", "fontsize": 10.0}
    if maxclass == "newobj":
        b["text"] = objtext
    if otype is not None:
        b["outlettype"] = otype
    b.update(extra)
    return b

def line(a, ao, b, bi):
    return {"patchline": {"source": [a["id"], ao], "destination": [b["id"], bi]}}

def gen_box(code, n_in, n_out, x, y):
    inner, lines = [], []
    cb = {"id": "cb", "maxclass": "codebox", "code": code, "fontface": 0, "fontname": "<Monospaced>",
          "fontsize": 12.0, "numinlets": n_in, "numoutlets": n_out, "outlettype": [""] * n_out,
          "patching_rect": [20, 60, 600, 500]}
    inner.append({"box": cb})
    for i in range(n_in):
        ib = {"id": "in%d" % (i + 1), "maxclass": "newobj", "text": "in %d" % (i + 1), "numinlets": 0,
              "numoutlets": 1, "outlettype": [""], "patching_rect": [20 + 80 * i, 20, 40, 22]}
        inner.append({"box": ib}); lines.append({"patchline": {"source": [ib["id"], 0], "destination": ["cb", i]}})
    for o in range(n_out):
        ob = {"id": "out%d" % (o + 1), "maxclass": "newobj", "text": "out %d" % (o + 1), "numinlets": 1,
              "numoutlets": 0, "patching_rect": [20 + 80 * o, 580, 45, 22]}
        inner.append({"box": ob}); lines.append({"patchline": {"source": ["cb", o], "destination": [ob["id"], 0]}})
    gp = {"fileversion": 1, "appversion": APPV, "classnamespace": "dsp.gen", "rect": [50, 50, 700, 650],
          "boxes": inner, "lines": lines}
    return box("gen~", x, y, 120, 22, inl=n_in, outl=n_out, otype=["signal"] * n_out, patcher=gp)

def dial(name, short, lo, hi, init, unit, x, y, px, py, typ=0, steps=None):
    # unitstyle: 0 int, 1 float, 2 time, 3 Hz, 4 dB, 5 %
    v = {"parameter_longname": name, "parameter_shortname": short, "parameter_type": typ,
         "parameter_mmin": lo, "parameter_mmax": hi, "parameter_initial": [init],
         "parameter_initial_enable": 1, "parameter_unitstyle": unit}
    b = box("", x, y, 44, 48, inl=1, outl=2, otype=["", "float"], maxclass="live.dial",
            parameter_enable=1, presentation=1, presentation_rect=[px, py, 44, 48], varname=name,
            saved_attribute_attributes={"valueof": v})
    return b

def comment(text, x, y, px, py, w=160):
    return box("", x, y, w, 18, inl=1, outl=0, maxclass="comment", text=text, presentation=1,
               presentation_rect=[px, py, w, 18])

TEMPLATE = "/Applications/Ableton Live 12 Trial.app/Contents/App-Resources/Misc/Max Devices/Max Audio Effect.amxd"
if not os.path.exists(TEMPLATE):
    TEMPLATE = "/Applications/Ableton Live 11 Suite.app/Contents/App-Resources/Misc/Max Devices/Max Audio Effect.amxd"

def _template():
    raw = open(TEMPLATE, "rb").read()
    n = struct.unpack("<I", raw[28:32])[0]
    return json.loads(raw[32:32 + n].decode("utf-8").rstrip("\x00"))

def patcher(boxes, lines, width):
    """Abletons eigene Audio-Effect-Vorlage als Basis (alle Pflichtfelder inkl. project/searchpath),
    nur Boxen, Linien, Breite und Präsentationsmodus werden ersetzt."""
    t = _template()
    p = t["patcher"]
    p["boxes"] = [{"box": b} for b in boxes]
    p["lines"] = lines
    p["devicewidth"] = width
    p["openinpresentation"] = 1
    return t

def write_amxd(path, pat):
    js = json.dumps(pat, ensure_ascii=False, indent=1).encode("utf-8")
    with open(path, "wb") as f:
        f.write(b"ampf" + struct.pack("<I", 4) + b"aaaa" + b"meta" + struct.pack("<I", 4) + b"\0\0\0\0"
                + b"ptch" + struct.pack("<I", len(js)) + js)

def build_bass():
    _id[0] = 0
    B, L = [], []
    pin = box("plugin~", 20, 20, 60, 22, inl=2, outl=2, otype=["signal", "signal"]); B.append(pin)
    pph = box("plugphasor~", 120, 20, 80, 22, inl=1, outl=1, otype=["signal"]); B.append(pph)
    g = gen_box(BASS_GEN, 3, 8, 20, 200); B.append(g)
    pout = box("plugout~", 20, 260, 60, 22, inl=2, outl=0); B.append(pout)
    L += [line(pin, 0, g, 0), line(pin, 1, g, 1), line(pph, 0, g, 2), line(g, 0, pout, 0), line(g, 1, pout, 1)]
    meter = box("", 200, 260, 10, 60, inl=1, outl=1, maxclass="live.meter~", presentation=1,
                presentation_rect=[300, 8, 10, 74]); B.append(meter); L.append(line(g, 2, meter, 0))
    ltd = box("live.thisdevice", 300, 20, 90, 22, inl=1, outl=3, otype=["bang", "int", "int"]); B.append(ltd)
    m5 = box("metro 5", 300, 50, 50, 22, inl=2, outl=1, otype=["bang"]); B.append(m5)
    m2 = box("metro 2000", 400, 50, 70, 22, inl=2, outl=1, otype=["bang"]); B.append(m2)
    # live.thisdevice bang -> metros starten + js scan
    tr = box("t b b b", 300, 80, 60, 22, inl=1, outl=3, otype=["bang", "bang", "bang"]); B.append(tr)
    msg1 = box("", 360, 110, 20, 20, inl=2, outl=1, otype=[""], maxclass="message", text="1"); B.append(msg1)
    L += [line(ltd, 0, tr, 0), line(tr, 2, msg1, 0), line(msg1, 0, m5, 0), line(msg1, 0, m2, 0)]
    ps = box("plugsync~", 300, 140, 70, 22, inl=1, outl=9,
             otype=["int", "int", "int", "float", "list", "float", "float", "int", "int"]); B.append(ps)
    L.append(line(m5, 0, ps, 0))
    pp = {}
    for nm, o, x in (("playing", 0, 300), ("tempo", 5, 380), ("ticks", 6, 460)):
        pb = box("prepend " + nm, x, 170, 75, 22, inl=1, outl=1, otype=[""]); B.append(pb)
        L += [line(ps, o, pb, 0), line(pb, 0, g, 0)]
    js = box("js lek_kick.js", 500, 110, 90, 22, inl=1, outl=2, otype=["", ""]); B.append(js)
    L += [line(tr, 0, js, 0), line(m2, 0, js, 0), line(js, 0, g, 0)]
    st = box("prepend set", 600, 140, 70, 22, inl=1, outl=1, otype=[""]); B.append(st)
    disp = comment("LEK Bass – startet …", 600, 170, 8, 64, w=280); B.append(disp)
    L += [line(js, 1, st, 0), line(st, 0, disp, 0)]
    buf = box("buffer~ lek_kickmap 100", 500, 20, 140, 22, inl=1, outl=2, otype=["float", "bang"]); B.append(buf)
    # Diagnose: gen~-Zustand alle 500 ms an js -> lek_debug.txt
    snaps = []
    for k, o in enumerate((3, 4, 5, 2, 6, 7)):
        sn = box("snapshot~ 500", 20 + 90 * k, 600, 80, 22, inl=2, outl=1, otype=["float"]); B.append(sn)
        L.append(line(g, o, sn, 0)); snaps.append(sn)
    pk6 = box("pak 0. 0. 0. 0. 0. 0.", 20, 640, 160, 22, inl=6, outl=1, otype=[""]); B.append(pk6)
    for k, sn in enumerate(snaps):
        L.append(line(sn, 0, pk6, k))
    pdbg = box("prepend dbg", 20, 670, 80, 22, inl=1, outl=1, otype=[""]); B.append(pdbg)
    L += [line(pk6, 0, pdbg, 0), line(pdbg, 0, js, 0)]
    rk = box("r lek_kicklen", 700, 20, 80, 22, inl=0, outl=1, otype=[""]); B.append(rk)
    pk = box("prepend kicklen", 700, 50, 90, 22, inl=1, outl=1, otype=[""]); B.append(pk)
    L += [line(rk, 0, pk, 0), line(pk, 0, g, 0)]
    # Regler
    specs = [("Weiche", "Xover", 40., 250., 120., 3, "xover", None, 8),
             ("Tiefe", "Depth", 0., 100., 100., 5, "depth", 100., 56),
             ("Halten", "Hold", 0., 50., 10., 2, "hold", 1000., 104),
             ("Release", "Rel %", 25., 300., 100., 5, "relpct", 100., 152),
             ("Vorlauf", "Pre", 0., 10., 3., 2, "pre", 1000., 200),
             ("Release fest", "Rel ms", 20., 500., 150., 2, "relman", 1000., 248)]
    y = 320
    for long, short, lo, hi, init, unit, par, div, px in specs:
        d = dial(long, short, lo, hi, init, unit, 20 + px * 2, y, px, 8, typ=0); B.append(d)
        chain = [d]
        if div:
            dv = box("/ %s" % div, 20 + px * 2, y + 60, 50, 22, inl=2, outl=1, otype=["float"]); B.append(dv)
            L.append(line(d, 0, dv, 0)); chain.append(dv)
        pr = box("prepend " + par, 20 + px * 2, y + 90, 80, 22, inl=1, outl=1, otype=[""]); B.append(pr)
        L += [line(chain[-1], 0, pr, 0), line(pr, 0, g, 0), line(tr, 1, d, 0)]
    tog = box("", 600, 320, 20, 20, inl=1, outl=1, otype=[""], maxclass="live.toggle", parameter_enable=1,
              presentation=1, presentation_rect=[248, 60, 16, 16], varname="Auto Release",
              saved_attribute_attributes={"valueof": {"parameter_longname": "Auto Release",
              "parameter_shortname": "Auto", "parameter_type": 2, "parameter_enum": ["aus", "an"],
              "parameter_mmax": 1, "parameter_initial": [1], "parameter_initial_enable": 1}}); B.append(tog)
    pa = box("prepend autorel", 600, 350, 90, 22, inl=1, outl=1, otype=[""]); B.append(pa)
    L += [line(tog, 0, pa, 0), line(pa, 0, g, 0), line(tr, 1, tog, 0)]
    B.append(comment("Auto", 630, 320, 266, 60, w=30))
    return patcher(B, L, 320.0)

def build_kick():
    _id[0] = 0
    B, L = [], []
    pin = box("plugin~", 20, 20, 60, 22, inl=2, outl=2, otype=["signal", "signal"]); B.append(pin)
    g = gen_box(KICK_GEN, 2, 4, 20, 120); B.append(g)
    pout = box("plugout~", 20, 200, 60, 22, inl=2, outl=0); B.append(pout)
    L += [line(pin, 0, g, 0), line(pin, 1, g, 1), line(g, 0, pout, 0), line(g, 1, pout, 1)]
    snap = box("snapshot~ 100", 200, 200, 80, 22, inl=2, outl=1, otype=["float"]); B.append(snap)
    ch = box("change 0.", 200, 230, 60, 22, inl=1, outl=3, otype=["", "int", "int"]); B.append(ch)
    s = box("s lek_kicklen", 200, 260, 80, 22, inl=1, outl=0); B.append(s)
    ms = box("* 1000.", 320, 230, 50, 22, inl=2, outl=1, otype=["float"]); B.append(ms)
    rnd = box("round 1", 320, 255, 50, 22, inl=2, outl=1, otype=["float"]); B.append(rnd)
    fm = box("sprintf set Kick-Länge: %.0f ms", 320, 280, 170, 22, inl=1, outl=1, otype=[""]); B.append(fm)
    disp = comment("Kick-Länge: – (abspielen)", 320, 310, 8, 8, w=180); B.append(disp)
    L += [line(g, 2, snap, 0), line(snap, 0, ch, 0), line(ch, 0, s, 0), line(ch, 0, ms, 0),
          line(ms, 0, rnd, 0), line(rnd, 0, fm, 0), line(fm, 0, disp, 0)]
    B.append(comment("sendet an LEK Bass", 500, 310, 8, 30, w=180))
    return patcher(B, L, 200.0)

if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    write_amxd(os.path.join(out, "LEK Bass.amxd"), build_bass())
    write_amxd(os.path.join(out, "LEK Kick.amxd"), build_kick())
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lek_kick.js"), os.path.join(out, "lek_kick.js"))
    print("ok", os.listdir(out))
