// LEK – Low-End-Kopplung: liest das Kick-Raster (Arrangement) aus Live und schreibt es in buffer~ lek_kickmap.
// ES5 (Max 8 / Live 11 kompatibel).
autowatch = 1;
inlets = 1;
outlets = 2;   // 0: Messages an gen~ (nkick, reset)   1: Statustext
var kickName = "KICK";
var myCounter = 0;

// Drossel (08.11.26): Früher scannte JEDE LEK-Instanz alle 2 s das ganze Set und legte dabei
// pro Spur/Clip neue LiveAPI-Objekte an -> Live-Hauptthread dauerhaft blockiert, Speicher wuchs auf GB.
// Jetzt: ein LiveAPI-Objekt pro Instanz wird wiederverwendet, und über ein gemeinsames Global
// scannt höchstens EINE Instanz alle SCAN_MS; die anderen übernehmen nur das Ergebnis.
var SCAN_MS = 15000;
var g = new Global("lek_shared");
if (g.counter === undefined) { g.counter = 0; g.n = 0; g.ti = -1; }
g.sig = ""; g.last = 0;   // beim (Neu-)Laden eines Geräts/Sets sofort frisch scannen (buffer~ kann leer sein)

var api = null;
function at(path) {
    if (!api) api = new LiveAPI(path); else api.path = path;
    return api;
}

function kicktrack(name) { if (name) kickName = String(name); g.sig = ""; g.ti = -1; g.last = 0; bang(); }

function str(v) { return String((v && v.length) ? v[0] : v); }

function findKickTrack() {
    var want = kickName.toLowerCase();
    // gemerkte Spur noch gültig? (eine Abfrage statt alle Spuren)
    if (g.ti >= 0) {
        try { if (str(at("live_set tracks " + g.ti).get("name")).toLowerCase() === want) return g.ti; } catch (e) {}
    }
    var n = at("live_set").getcount("tracks");
    var fallback = -1;
    for (var i = 0; i < n; i++) {
        var t = at("live_set tracks " + i);
        var nm = str(t.get("name")).toLowerCase();
        if (nm === want) { g.ti = i; return i; }
        if (fallback < 0 && nm.indexOf(want) >= 0 && t.get("has_midi_input") == 1) fallback = i;
    }
    g.ti = fallback;
    return fallback;
}

function num(a, prop) { var v = a.get(prop); return (v && v.length) ? Number(v[0]) : Number(v); }

function publish() {
    if (myCounter === g.counter) return;
    myCounter = g.counter;
    outlet(0, "nkick", g.n);
    outlet(0, "rescan", g.counter);
    outlet(1, "Kick: " + kickName + " · " + g.n + " Schläge");
}

function scan() {
    var ti = findKickTrack();
    if (ti < 0) { outlet(1, "Keine Kick-Spur '" + kickName + "' gefunden"); return; }
    var tpath = "live_set tracks " + ti;
    var nc = at(tpath).getcount("arrangement_clips");
    var hits = [];
    for (var c = 0; c < nc; c++) {
        var clip = at(tpath + " arrangement_clips " + c);
        if (num(clip, "is_midi_clip") != 1) continue;
        if (num(clip, "muted") == 1) continue;
        var st = num(clip, "start_time"), en = num(clip, "end_time");
        var looping = num(clip, "looping") == 1;
        var ls = num(clip, "loop_start"), le = num(clip, "loop_end"), sm = num(clip, "start_marker");
        var raw = clip.call("get_notes_extended", 0, 128, 0, 100000);
        var notes;
        try { notes = JSON.parse(raw).notes; } catch (e) { continue; }
        var L = le - ls;
        for (var k = 0; k < notes.length; k++) {
            var nt = notes[k];
            if (nt.mute) continue;
            var t0 = nt.start_time;
            if (!looping || L <= 0) {
                var a = st + (t0 - sm);
                if (t0 >= sm && a < en) hits.push(a);
            } else {
                if (t0 >= sm && t0 < ls) { var b = st + (t0 - sm); if (b < en) hits.push(b); }
                if (t0 >= ls && t0 < le) {
                    var first = st + (t0 - sm);
                    var kk = 0;
                    if (t0 < sm) kk = Math.ceil((sm - t0) / L);
                    for (var p = first + kk * L; p < en; p += L) hits.push(p);
                }
            }
        }
    }
    hits.sort(function (x, y) { return x - y; });
    // Doppelte (Layer-Noten) zusammenfassen
    var uniq = [];
    for (var h = 0; h < hits.length; h++) if (!uniq.length || hits[h] - uniq[uniq.length - 1] > 0.01) uniq.push(hits[h]);
    var sig = uniq.length + ":" + (uniq.length ? uniq[0] + ":" + uniq[uniq.length - 1] : "") + ":" + uniq.join(",").length;
    if (sig === g.sig) return;
    g.sig = sig;
    var buf = new Buffer("lek_kickmap");
    buf.send("sizeinsamps", Math.max(64, uniq.length + 16));
    if (uniq.length) buf.poke(1, 0, uniq);
    g.n = uniq.length;
    g.counter++;
    log("scan track=" + ti + " clips=" + nc + " hits=" + uniq.length + " first=" + uniq.slice(0, 6).join(","));
}

var DEBUG = false;   // true = Zustand nach lek_debug.txt schreiben
var dbgCount = 0;
function log(msg) {
    if (!DEBUG) return;
    try {
        var f = new File("lek_debug.txt", "readwrite");
        if (f.isopen) { f.position = f.eof; f.writeline(new Date().toISOString() + " " + msg); f.close(); }
    } catch (e2) {}
}
// gen~-Zustand (pos, nkick, letzter Kick, Duck-Anteil, Tempo) – kommt alle 500 ms als Liste
function dbg() { if (!DEBUG) return; dbgCount++; if (dbgCount > 60 && dbgCount % 20 != 0) return; var a = arrayfromargs(arguments); log("gen pos=" + a[0].toFixed(3) + " nkick=" + a[1] + " last=" + a[2].toFixed(3) + " duck=" + a[3].toFixed(2) + " playing=" + a[4] + " kicklen=" + a[5].toFixed(3)); }
// kommt alle 2 s vom metro jeder Instanz – gescannt wird nur, wenn fällig
function bang() {
    var now = new Date().getTime();
    if (now - g.last >= SCAN_MS) { g.last = now; scan(); }
    publish();
}
log("js geladen");
