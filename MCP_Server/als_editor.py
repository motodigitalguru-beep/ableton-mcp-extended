"""
als_editor.py — Live-Set-Datei (.als) direkt bearbeiten.

Für alles, was die Live-API nicht freigibt (Gruppen bilden, Spuren umsortieren,
Sidechain-Quellen, Routing). Eine .als ist gzip-komprimiertes XML.

Sicherheitsprinzip: das Original wird nie überschrieben. Bearbeitet wird eine
Arbeitskopie "<Name> MCP.als" im selben Ordner; ist die bereits geöffnet, wandert
ihr alter Stand vorher nach Backup/. Danach öffnet Live die Kopie.

Fallen im Format, die hier behandelt werden:
- Automations-/Modulationsziele (Tags auf "...Target" und "Pointee") tragen
  projektweit eindeutige Ids unterhalb von LiveSet/NextPointeeId. Wer einen
  Teilbaum klont, muss sie neu vergeben, sonst kollidieren Automationen.
- Spur-Ids (GroupTrack/MidiTrack/AudioTrack Id) sind ein eigener Zahlenraum.
- Gruppen müssen zusammenhängend direkt hinter ihrer GroupTrack stehen.
"""
from __future__ import annotations

import copy
import gzip
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

LIVE_LOG_GLOB = os.path.expanduser("~/Library/Preferences/Ableton")
TRACK_TAGS = {"GroupTrack", "MidiTrack", "AudioTrack"}
TAPS = {"pre": ("PreFxOut", "Pre FX"), "post": ("PostFxOut", "Post FX"), "mixer": ("TrackOut", "Post Mixer")}


# ── Datei ───────────────────────────────────────────────────────────────────
def current_set_path() -> str | None:
    """Zuletzt von Live geladenes Dokument laut Log.txt (neueste Live-Version)."""
    best = None
    for root, _dirs, files in os.walk(LIVE_LOG_GLOB):
        if "Log.txt" in files:
            p = os.path.join(root, "Log.txt")
            if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                best = p
    if not best:
        return None
    last = None
    with open(best, "rb") as f:
        for line in f:
            m = re.search(rb'Loading document "([^"]+\.als)"', line)
            if (m and b"/Contents/App-Resources/" not in m.group(1) and b"/Builtin/Templates/" not in m.group(1)
                    and b"/BaseFiles/" not in m.group(1) and b"/Library/Preferences/Ableton/" not in m.group(1)):
                # BaseFiles = Abletons interne Kopie (Wiederherstellung/Undo) – nie bearbeiten
                last = m.group(1).decode("utf-8", "replace")
    # „Speichern unter“ steht nicht im Log: neuere .als im selben Projektordner bevorzugen.
    if last and os.path.exists(last):
        folder = os.path.dirname(last)
        newer = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".als")]
        newest = max(newer, key=os.path.getmtime)
        if os.path.getmtime(newest) > os.path.getmtime(last):
            last = newest
    return last


def load(path: str) -> ET.ElementTree:
    with gzip.open(path, "rb") as f:
        return ET.ElementTree(ET.fromstring(f.read()))


def write(tree: ET.ElementTree, path: str) -> None:
    data = ET.tostring(tree.getroot(), encoding="UTF-8", xml_declaration=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def working_copy_path(src: str) -> str:
    folder, name = os.path.split(src)
    stem = name[:-4]
    if stem.endswith(" MCP"):
        bdir = os.path.join(folder, "Backup")
        os.makedirs(bdir, exist_ok=True)
        shutil.copy2(src, os.path.join(bdir, "%s [%s].als" % (stem, time.strftime("%Y-%m-%d %H%M%S"))))
        return src
    return os.path.join(folder, stem + " MCP.als")


# ── Live-Bedienung (Speichern / Öffnen) ─────────────────────────────────────
def save_in_live(path_hint: str | None = None, timeout: float = 12.0) -> dict:
    before = os.path.getmtime(path_hint) if path_hint and os.path.exists(path_hint) else None
    script = ('tell application "System Events" to set p to name of first process whose name starts with "Live"\n'
              'tell application p to activate\n'
              'delay 0.3\n'
              'tell application "System Events" to keystroke "s" using command down')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or "osascript failed",
                "hint": "System Settings > Privacy & Security > Accessibility: allow the app that runs the MCP server"}
    saved = None
    if before is not None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.getmtime(path_hint) > before:
                saved = True
                break
            time.sleep(0.3)
        saved = bool(saved)
    return {"ok": True, "file_changed": saved}


def open_in_live(path: str) -> dict:
    r = subprocess.run(["open", path], capture_output=True, text=True, timeout=20)
    return {"ok": r.returncode == 0, "error": r.stderr.strip() or None}


# ── Struktur-Helfer ─────────────────────────────────────────────────────────
def _liveset(tree):
    return tree.getroot().find("LiveSet")


def _tracks_el(tree):
    return _liveset(tree).find("Tracks")


def track_list(tree):
    return [t for t in _tracks_el(tree) if t.tag in TRACK_TAGS]


def track_name(t) -> str:
    n = t.find("Name/EffectiveName")
    return n.get("Value") if n is not None else ""


def _set_name(t, name):
    for tag in ("EffectiveName", "UserName"):
        e = t.find("Name/" + tag)
        if e is not None:
            e.set("Value", name)


def find_track(tree, ref):
    """ref: API-Index (int, Reihenfolge wie get_track_info) oder exakter/teilweiser Name."""
    tl = track_list(tree)
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return tl[int(ref)]
    exact = [t for t in tl if track_name(t) == ref]
    if exact:
        return exact[0]
    part = [t for t in tl if ref.lower() in track_name(t).lower()]
    if len(part) == 1:
        return part[0]
    rets = [t for t in _tracks_el(tree) if t.tag == "ReturnTrack"]   # Return-Spuren nur per Name
    rx = [t for t in rets if track_name(t) == ref] or [t for t in rets if ref.lower() in track_name(t).lower()]
    if len(rx) == 1:
        return rx[0]
    raise ValueError("Track '%s' not found or ambiguous (%d matches)" % (ref, len(part)))


def _group_of(t) -> str:
    return t.find("TrackGroupId").get("Value")


def _route(el, target, upper, lower=""):
    el.find("Target").set("Value", target)
    el.find("UpperDisplayString").set("Value", upper)
    el.find("LowerDisplayString").set("Value", lower)


def _output_el(t):
    return t.find("DeviceChain/AudioOutputRouting")


def _input_el(t):
    return t.find("DeviceChain/AudioInputRouting")


def _devices(t):
    d = t.find("DeviceChain/DeviceChain/Devices")
    return list(d) if d is not None else []


def _renumber_pointees(tree, subtree) -> int:
    ls = _liveset(tree)
    npi = ls.find("NextPointeeId")
    nxt = int(npi.get("Value"))
    n = 0
    for e in subtree.iter():
        if e.get("Id") is not None and (e.tag.endswith("Target") or e.tag == "Pointee"):
            e.set("Id", str(nxt))
            nxt += 1
            n += 1
    npi.set("Value", str(nxt))
    return n


def _next_track_id(tree) -> int:
    ids = []
    for e in _tracks_el(tree):
        if e.get("Id") is not None:
            ids.append(int(e.get("Id")))
    for tag in ("MainTrack", "MasterTrack", "PreHearTrack"):
        e = _liveset(tree).find(tag)
        if e is not None and e.get("Id") is not None:
            ids.append(int(e.get("Id")))
    return max(ids) + 1


def _route_to_parent(t, parent_group_id: str):
    out = _output_el(t)
    if out is None:
        return
    if parent_group_id != "-1":
        _route(out, "AudioOut/GroupTrack", "Group")
    else:
        _route(out, "AudioOut/Main", "Main")


def _foreign_group_template(tree):
    """GroupTrack aus einem anderen Set als Vorlage, wenn das aktuelle keine hat.
    Nur Sets derselben Live-Version mit gleicher Szenen- und Return-Anzahl kommen in Frage
    (GroupTrackSlots und Send-Halter müssen 1:1 passen). Suche: ~/Desktop/*/*.als, neueste zuerst."""
    import glob
    root = tree.getroot()
    ls = _liveset(tree)
    want = (root.get("Creator"), len(ls.find("Scenes")),
            len([t for t in _tracks_el(tree) if t.tag == "ReturnTrack"]))
    cands = sorted(glob.glob(os.path.expanduser("~/Desktop/*/*.als")), key=os.path.getmtime, reverse=True)
    for c in cands:
        try:
            ot = load(c)
        except Exception:
            continue
        ols = ot.getroot().find("LiveSet")
        got = (ot.getroot().get("Creator"), len(ols.find("Scenes")),
               len([t for t in ols.find("Tracks") if t.tag == "ReturnTrack"]))
        if got != want:
            continue
        g = next((t for t in ols.find("Tracks") if t.tag == "GroupTrack"), None)
        if g is not None:
            return g
    return None


# ── Operationen ─────────────────────────────────────────────────────────────
def op_group(tree, tracks, name):
    tl_el = _tracks_el(tree)
    members = [find_track(tree, r) for r in tracks]
    if not members:
        raise ValueError("no tracks to group")
    parents = {_group_of(m) for m in members}
    parent = parents.pop() if len(parents) == 1 else "-1"
    template = next((t for t in tl_el if t.tag == "GroupTrack"), None)
    if template is None:
        template = _foreign_group_template(tree)
    if template is None:
        raise ValueError("Set has no GroupTrack to use as template — create one group in Live first")
    g = copy.deepcopy(template)
    gid = str(_next_track_id(tree))
    g.set("Id", gid)
    _set_name(g, name)
    g.find("TrackGroupId").set("Value", parent)
    ltg = g.find("LinkedTrackGroupId")
    if ltg is not None:
        ltg.set("Value", "-1")
    devs = g.find("DeviceChain/DeviceChain/Devices")
    if devs is not None:
        for d in list(devs):
            devs.remove(d)
    envs = g.find("AutomationEnvelopes/Envelopes")
    if envs is not None:
        for e in list(envs):
            envs.remove(e)
    mixer = g.find("DeviceChain/Mixer")
    if mixer is not None:
        for path, val in (("Volume/Manual", "1"), ("Pan/Manual", "0"), ("Speaker/Manual", "true")):
            e = mixer.find(path)
            if e is not None:
                e.set("Value", val)
        for s in mixer.findall("Sends/TrackSendHolder/Send/Manual"):
            s.set("Value", "0.0003162277571")
    _route_to_parent(g, parent)
    _renumber_pointees(tree, g)
    # Reihenfolge: Gruppe an die Stelle des ersten Mitglieds, Mitglieder direkt dahinter
    children = list(tl_el)
    first = min(children.index(m) for m in members)
    ordered = sorted(members, key=children.index)
    for m in ordered:
        tl_el.remove(m)
    tl_el.insert(first, g)
    for k, m in enumerate(ordered):
        tl_el.insert(first + 1 + k, m)
        m.find("TrackGroupId").set("Value", gid)
        _route(_output_el(m), "AudioOut/GroupTrack", "Group")
    return {"group": name, "id": gid, "members": [track_name(m) for m in ordered]}


def op_ungroup(tree, group):
    tl_el = _tracks_el(tree)
    g = find_track(tree, group)
    if g.tag != "GroupTrack":
        raise ValueError("'%s' is not a group" % track_name(g))
    gid, parent = g.get("Id"), _group_of(g)
    kids = [t for t in tl_el if t.tag in TRACK_TAGS and _group_of(t) == gid]
    for k in kids:
        k.find("TrackGroupId").set("Value", parent)
        _route_to_parent(k, parent)
    lost = [d.tag for d in _devices(g)]
    tl_el.remove(g)
    return {"ungrouped": track_name(g), "members": [track_name(k) for k in kids], "group_devices_removed": lost}


def op_move(tree, track, before=None, after=None, into_group=None):
    tl_el = _tracks_el(tree)
    t = find_track(tree, track)
    block = [t]
    if t.tag == "GroupTrack":   # Gruppe samt Inhalt bewegen
        gid = t.get("Id")
        def inside(x, gids={gid}):
            g = _group_of(x)
            if g in gids:
                if x.tag == "GroupTrack":
                    gids.add(x.get("Id"))
                return True
            return False
        block += [x for x in tl_el if x is not t and x.tag in TRACK_TAGS and inside(x)]
    for b in block:
        tl_el.remove(b)
    ref = find_track(tree, before if before is not None else after)
    pos = list(tl_el).index(ref)
    if after is not None:
        if ref.tag == "GroupTrack" and into_group is None:   # hinter eine Gruppe = hinter ihr letztes Mitglied
            rid = ref.get("Id")
            while pos + 1 < len(tl_el) and _group_of(list(tl_el)[pos + 1]) in (rid,):
                pos += 1
        pos += 1
    for k, b in enumerate(block):
        tl_el.insert(pos + k, b)
    if into_group is not None:
        g = find_track(tree, into_group)
        new_parent = g.get("Id")
    else:
        new_parent = _group_of(ref)
    t.find("TrackGroupId").set("Value", new_parent)
    _route_to_parent(t, new_parent)
    return {"moved": track_name(t), "with": len(block) - 1, "group_id": new_parent}


def device_label(d) -> str:
    """Anzeigename: UserName > Plugin-Name > XML-Tag."""
    un = d.find("UserName")
    if un is not None and un.get("Value"):
        return un.get("Value")
    for path in ("PluginDesc/Vst3PluginInfo/Name", "PluginDesc/VstPluginInfo/PlugName", "PluginDesc/AuPluginInfo/Name"):
        e = d.find(path)
        if e is not None and e.get("Value"):
            return e.get("Value")
    return d.tag


def _device(t, device):
    devs = _devices(t)
    if isinstance(device, int) or (isinstance(device, str) and device.isdigit()):
        return devs[int(device)]
    cand = [d for d in devs if d.tag.lower() == str(device).lower() or device_label(d).lower() == str(device).lower()]
    if not cand:
        raise ValueError("Device '%s' not found on '%s' (%s)" % (device, track_name(t), ", ".join(d.tag for d in devs)))
    return cand[0]


def op_sidechain(tree, track, device, source, tap="pre", enabled=True):
    t = find_track(tree, track)
    d = _device(t, device)
    sc = d.find("SideChain")
    if sc is None:
        raise ValueError("Device '%s' has no SideChain section" % d.tag)
    src = find_track(tree, source)
    tgt, lower = TAPS[tap]
    _route(sc.find("RoutedInput/Routable"), "AudioIn/Track.%s/%s" % (src.get("Id"), tgt), track_name(src), lower)
    onoff = sc.find("OnOff/Manual")
    if onoff is not None:
        onoff.set("Value", "true" if enabled else "false")
    return {"track": track_name(t), "device": d.tag, "source": track_name(src), "tap": lower, "enabled": enabled}


def op_route_input(tree, track, source, tap="post"):
    t = find_track(tree, track)
    src = find_track(tree, source)
    tgt, lower = TAPS[tap]
    _route(_input_el(t), "AudioIn/Track.%s/%s" % (src.get("Id"), tgt), track_name(src), lower)
    return {"track": track_name(t), "input": track_name(src), "tap": lower}


def op_route_output(tree, track, to="main"):
    t = find_track(tree, track)
    if str(to).lower() in ("main", "master"):
        _route(_output_el(t), "AudioOut/Main", "Main")
    elif str(to).lower() == "group":
        if _group_of(t) == "-1":
            raise ValueError("'%s' is not inside a group" % track_name(t))
        _route(_output_el(t), "AudioOut/GroupTrack", "Group")
    else:
        raise ValueError("to must be 'main' or 'group' (track-to-track output: use Live's Audio To)")
    return {"track": track_name(t), "output": to}


def op_rename(tree, track, name):
    t = find_track(tree, track)
    old = track_name(t)
    _set_name(t, name)
    return {"renamed": old, "to": name}


def _plugin_preset(d):
    for path in ("PluginDesc/Vst3PluginInfo/Preset", "PluginDesc/VstPluginInfo/Preset", "PluginDesc/AuPluginInfo/Preset"):
        p = d.find(path)
        if p is not None and len(p):
            return p[0]
    raise ValueError("Device '%s' is not a plugin with stored state" % device_label(d))


def op_copy_plugin_state(tree, source_track, target_track, device=0, target_device=None, source_file=None):
    """Klangzustand (Processor-/Controller-State bzw. VST2-Buffer) eines Plugins auf ein
    gleiches Plugin einer anderen Spur übertragen. Configure-Freigaben des Ziels bleiben.
    source_file: Quelle aus einer anderen .als (z. B. Backup vor dem Configure)."""
    src_tree = load(source_file) if source_file else tree
    sd = _device(find_track(src_tree, source_track), device)
    td = _device(find_track(tree, target_track), device if target_device is None else target_device)
    sp, tp = _plugin_preset(sd), _plugin_preset(td)
    if device_label(sd) != device_label(td):
        raise ValueError("Different plugins: '%s' vs '%s'" % (device_label(sd), device_label(td)))
    copied = []
    for tag in ("ProcessorState", "ControllerState", "Buffer"):
        s, t = sp.find(tag), tp.find(tag)
        if s is not None and t is not None:
            t.text = s.text
            copied.append(tag)
    return {"plugin": device_label(sd), "copied": copied}


def op_copy_param_config(tree, source_track, source_device, target_track, target_device):
    """Configure-Freigabe (welche Plugin-Parameter in Live sichtbar sind) von einem Plugin auf
    ein gleiches Plugin übertragen. Slot-weise: nur Name/Id/Reihenfolge, Automation-Ids und
    Klangzustand des Ziels bleiben unangetastet."""
    sd = _device(find_track(tree, source_track), source_device)
    td = _device(find_track(tree, target_track), target_device)
    if device_label(sd) != device_label(td):
        raise ValueError("Different plugins: '%s' vs '%s'" % (device_label(sd), device_label(td)))
    sl, tl_ = list(sd.find("ParameterList")), list(td.find("ParameterList"))
    if len(sl) != len(tl_) or any(a.tag != b.tag for a, b in zip(sl, tl_)):
        raise ValueError("Parameter lists differ in layout")
    n = 0
    for s, t in zip(sl, tl_):
        for tag in ("ParameterName", "ParameterId", "ParameterIdFlankBool", "VisualIndex"):
            se, te = s.find(tag), t.find(tag)
            if se is not None and te is not None:
                te.set("Value", se.get("Value"))
        n += s.find("ParameterId").get("Value") != "-1"
    return {"plugin": device_label(sd), "target": track_name(find_track(tree, target_track)), "configured": n}


def op_copy_device(tree, source_file, source_track, source_device, target_track, replace_device=None):
    """Gerät (inkl. Klangzustand UND Configure-Freigabe) aus einer anderen .als übernehmen.
    replace_device: Index/Name eines Geräts auf der Zielspur, das ersetzt wird; sonst ans Kettenende.
    Automations-/Modulations-Ids werden neu vergeben, damit sie im Ziel-Set eindeutig sind."""
    import copy
    src = copy.deepcopy(_device(find_track(load(source_file), source_track), source_device))
    t = find_track(tree, target_track)
    devs = t.find("DeviceChain/DeviceChain/Devices")
    ids = [int(d.get("Id")) for d in devs if d.get("Id", "").isdigit()]
    src.set("Id", str(max(ids) + 1 if ids else 0))
    n = _renumber_pointees(tree, src)
    for e in src.iter("LomId"):
        e.set("Value", "0")
    if replace_device is not None:
        old = _device(t, replace_device)
        pos = list(devs).index(old)
        devs.remove(old)
        devs.insert(pos, src)
    else:
        devs.append(src)
    return {"copied": device_label(src), "into": track_name(t), "pointees_renumbered": n,
            "replaced": replace_device is not None}


def op_rack_selector(tree, track, macro_name="Auswahl", macro_index=0, device=None):
    """Instrument Rack einer Spur: Kette i bekommt Chain-Selector-Zone i..i (ohne Überblendung),
    Makro <macro_index> (Standard Makro 1) wird auf den Chain Selector gemappt (Bereich 0..n-1)
    und heißt <macro_name>. Ableton speichert Makro-Mappings als KeyMidi auf Kanal 16."""
    t = find_track(tree, track)
    racks = [d for d in t.iter("InstrumentGroupDevice")]
    if not racks:
        raise ValueError("no Instrument Rack on track '%s'" % track_name(t))
    rack = racks[int(device or 0)]
    branches = rack.findall("Branches/InstrumentBranch")
    n = len(branches)
    for i, b in enumerate(branches):
        r = b.find("BranchSelectorRange")
        for tag, v in (("Min", i), ("Max", i), ("CrossfadeMin", i), ("CrossfadeMax", i)):
            r.find(tag).set("Value", str(v))
    cs = rack.find("ChainSelector")
    km = cs.find("KeyMidi")
    if km is None:
        km = ET.Element("KeyMidi")
        for tag, v in (("PersistentKeyString", ""), ("IsNote", "false"), ("Channel", "16"),
                       ("NoteOrController", str(macro_index)), ("LowerRangeNote", "-1"),
                       ("UpperRangeNote", "-1"), ("ControllerMapMode", "0")):
            ET.SubElement(km, tag).set("Value", v)
        kids = list(cs)
        cs.insert(kids.index(cs.find("LomId")) + 1 if cs.find("LomId") is not None else 0, km)
    else:
        km.find("Channel").set("Value", "16")
        km.find("NoteOrController").set("Value", str(macro_index))
        km.find("IsNote").set("Value", "false")
    cs.find("Manual").set("Value", "0")
    rng = cs.find("MidiControllerRange")
    rng.find("Min").set("Value", "0")
    rng.find("Max").set("Value", str(n - 1))
    rack.find("MacroDisplayNames.%d" % macro_index).set("Value", macro_name)
    mc = rack.find("MacroControls.%d" % macro_index)
    mc.find("Manual").set("Value", "0")
    return {"track": track_name(t), "chains": n, "macro": "%d=%s" % (macro_index + 1, macro_name)}


def op_automation(tree, track, device, param, points, default=None, replace=True):
    """Arrangement-Automation schreiben (die Live-API kann das nicht).
    device: Geräte-Tag oder Nutzername, optional '#n' (n-tes Vorkommen, 1-basiert), z. B. 'Eq8' oder 'Eq8#1'.
    param: Pfad im Geräte-XML zum Parameter, z. B. 'Bands.3/ParameterA/Freq'.
    points: [[beat, value], ...] in Arrangement-Beats und echten Parameterwerten (z. B. Hz).
    default: Wert vor dem ersten Punkt (sonst erster Punktwert). replace: vorhandene Hüllkurve ersetzen."""
    t = find_track(tree, track)
    want, _, nth = str(device).partition("#")
    nth = int(nth or 1)
    devs = t.find("DeviceChain/DeviceChain/Devices")
    if want == "Mixer":   # Mixer der Spur, z. B. param 'Sends/TrackSendHolder[3]/Send' (1-basiert), 'Volume', 'Pan'
        hits = [t.find("DeviceChain/Mixer")]
    else:
        hits = [d for d in devs if d.tag == want or ((d.find("UserName").get("Value") if d.find("UserName") is not None else "") == want)]
    if len(hits) < nth:
        raise ValueError("device '%s' not found on '%s'" % (device, track_name(t)))
    dev = hits[nth - 1]
    target = dev.find(param + "/AutomationTarget")
    if target is None:
        raise ValueError("parameter '%s' has no AutomationTarget" % param)
    pid = target.get("Id")
    envs = t.find("AutomationEnvelopes/Envelopes")
    for e in list(envs):
        pe = e.find("EnvelopeTarget/PointeeId")
        if pe is not None and pe.get("Value") == pid:
            if not replace:
                raise ValueError("envelope exists")
            envs.remove(e)
    eid = max([int(e.get("Id")) for e in envs] + [-1]) + 1
    next_ev = max([int(e.get("Id")) for e in tree.getroot().iter("FloatEvent")] + [0]) + 1
    env = ET.SubElement(envs, "AutomationEnvelope", {"Id": str(eid)})
    ET.SubElement(ET.SubElement(env, "EnvelopeTarget"), "PointeeId").set("Value", pid)
    auto = ET.SubElement(env, "Automation")
    evs = ET.SubElement(auto, "Events")
    pts = sorted(([float(b), float(v)] for b, v in points), key=lambda x: x[0])
    first = default if default is not None else pts[0][1]
    for k, (b, v) in enumerate([[-63072000.0, first]] + pts):
        ET.SubElement(evs, "FloatEvent", {"Id": str(next_ev + k), "Time": ("%g" % b), "Value": ("%g" % v)})
    vs = ET.SubElement(auto, "AutomationTransformViewState")
    ET.SubElement(vs, "IsTransformPending").set("Value", "false")
    ET.SubElement(vs, "TimeAndValueTransforms")
    return {"track": track_name(t), "device": device, "param": param, "points": len(pts), "pointee": pid}


def op_clear_automation(tree, track, param="Volume", value=None):
    """Arrangement-Automation eines Mixer-Parameters löschen (track='Master' für den Master).
    param: Pfad unter DeviceChain/Mixer, z. B. 'Volume', 'Pan', 'Sends/TrackSendHolder[3]/Send'.
    value: neuer fester Wert (XML-Einheit, Volume 1.0 = 0 dB), sonst bleibt der Manual-Wert."""
    ls = _liveset(tree)
    t = (ls.find("MainTrack") if ls.find("MainTrack") is not None else ls.find("MasterTrack")) \
        if str(track).lower() in ("master", "main") else find_track(tree, track)
    p = t.find("DeviceChain/Mixer/" + param)
    pid = p.find("AutomationTarget").get("Id")
    envs = t.find("AutomationEnvelopes/Envelopes")
    removed = 0
    for e in list(envs):
        pe = e.find("EnvelopeTarget/PointeeId")
        if pe is not None and pe.get("Value") == pid:
            envs.remove(e); removed += 1
    if value is not None:
        p.find("Manual").set("Value", "%g" % float(value))
    return {"track": str(track), "param": param, "removed": removed, "manual": p.find("Manual").get("Value")}


OPS = {"automation": op_automation, "clear_automation": op_clear_automation, "rack_selector": op_rack_selector, "group": op_group, "ungroup": op_ungroup, "move": op_move, "sidechain": op_sidechain,
       "route_input": op_route_input, "route_output": op_route_output, "rename": op_rename,
       "copy_plugin_state": op_copy_plugin_state, "copy_param_config": op_copy_param_config,
       "copy_device": op_copy_device}


def info(tree) -> dict:
    out = []
    names = {t.get("Id"): track_name(t) for t in track_list(tree)}
    for i, t in enumerate(track_list(tree)):
        g = _group_of(t)
        row = {"index": i, "type": t.tag, "name": track_name(t), "id": t.get("Id"),
               "group": names.get(g) if g != "-1" else None,
               "devices": [device_label(d) for d in _devices(t)]}
        o = _output_el(t)
        if o is not None:
            row["output"] = o.find("UpperDisplayString").get("Value")
        i_el = _input_el(t)
        if i_el is not None and "Track." in i_el.find("Target").get("Value"):
            row["input"] = "%s / %s" % (i_el.find("UpperDisplayString").get("Value"), i_el.find("LowerDisplayString").get("Value"))
        scs = []
        for k, d in enumerate(_devices(t)):
            sc = d.find("SideChain")
            if sc is not None:
                on = sc.find("OnOff/Manual")
                r = sc.find("RoutedInput/Routable")
                scs.append({"device_index": k, "device": device_label(d), "on": on.get("Value") if on is not None else None,
                            "source": "%s / %s" % (r.find("UpperDisplayString").get("Value"), r.find("LowerDisplayString").get("Value"))
                            if r is not None else None})
        if scs:
            row["sidechains"] = scs
        out.append(row)
    return {"tracks": out}


def apply(operations: list[dict], path: str | None = None, save_first: bool = True, open_after: bool = True) -> dict:
    src = path or current_set_path()
    if not src or not os.path.exists(src):
        raise FileNotFoundError("Current Live Set not found (pass path=...)")
    saved = save_in_live(src) if save_first else None
    tree = load(src)
    results = []
    for op in operations:
        kind = op.get("op")
        fn = OPS.get(kind)
        if fn is None:
            raise ValueError("Unknown op '%s' (allowed: %s)" % (kind, ", ".join(OPS)))
        args = {k: v for k, v in op.items() if k != "op"}
        results.append({"op": kind, **fn(tree, **args)})
    dst = working_copy_path(src)
    write(tree, dst)
    opened = open_in_live(dst) if open_after else None
    return {"source": src, "written": dst, "saved_first": saved, "opened": opened, "results": results}
