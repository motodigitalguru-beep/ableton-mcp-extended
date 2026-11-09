"""Lehr-Template aus einem fertigen Live-Set bauen (ohne das laufende Set anzufassen).

- entfernt Referenz-/Hilfsspuren, leert Audio-Clips (Samples gehören ins Projekt, nicht ins Template)
- kürzt jede MIDI-Spur auf EIN Beispiel-Pattern ab Takt 1 (Phase des Loops bleibt erhalten)
- entfernt Arrangement-Automation, setzt Loop auf das Beispiel
- schreibt Erklärtexte in Spur- und Geräte-Info (Ableton Info-Ansicht) und Locators als Fahrplan
Aufruf siehe __main__ bzw. build(spec) mit einem Spec-Dict.
"""
import copy, os, sys
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from MCP_Server import als_editor as A


def _clips(track):
    ev = track.find(".//ArrangerAutomation/Events")
    return ev, (list(ev) if ev is not None else [])


def _set(el, path, value):
    e = el.find(path)
    if e is not None:
        e.set("Value", str(value))
    return e is not None


def build(spec):
    t = A.load(spec["source"])
    ls = t.getroot().find("LiveSet")
    tl = ls.find("Tracks")
    log = []
    # 1) Spuren entfernen
    for tr in list(tl):
        if tr.tag in ("AudioTrack", "MidiTrack", "GroupTrack") and any(
                s.lower() in A.track_name(tr).lower() for s in spec.get("drop_tracks", [])):
            tl.remove(tr); log.append("entfernt: " + A.track_name(tr))
    bars = spec.get("example_beats", 32)
    for tr in tl:
        if tr.tag not in ("AudioTrack", "MidiTrack"):
            continue
        name = A.track_name(tr)
        ev, cl = _clips(tr)
        if tr.tag == "AudioTrack":
            for c in cl:
                ev.remove(c)
            # Session-Audio-Clips ebenfalls entfernen
            for slot in tr.findall(".//ClipSlotList/ClipSlot/ClipSlot/Value"):
                for c in list(slot):
                    slot.remove(c)
        else:
            keep = None
            for c in cl:
                if keep is None and c.find("Loop/LoopOn") is not None:
                    keep = c
            for c in cl:
                if c is not keep:
                    ev.remove(c)
            if keep is not None:
                keep.set("Time", "0")
                _set(keep, "CurrentStart", 0)
                _set(keep, "CurrentEnd", bars)
        # Arrangement-Automation der Spur entfernen
        envs = tr.find("AutomationEnvelopes/Envelopes")
        if envs is not None:
            for e in list(envs):
                envs.remove(e)
        # neuer Name
        if name in spec.get("rename", {}):
            _set(tr, "Name/UserName", spec["rename"][name]); _set(tr, "Name/EffectiveName", spec["rename"][name])
    # 2) Infotexte Spuren + Geräte
    for tr in tl:
        if tr.tag not in ("AudioTrack", "MidiTrack", "GroupTrack"):
            continue
        name = A.track_name(tr)
        for key, text in spec.get("track_info", {}).items():
            if key.lower() in name.lower():
                _set(tr, "Name/Annotation", text); log.append("Info Spur: " + name)
        devs = tr.find("DeviceChain/DeviceChain/Devices")
        if devs is None:
            continue
        seen = {}
        for d in devs:
            label = (d.find("UserName").get("Value") if d.find("UserName") is not None else "") or d.tag
            seen[label] = seen.get(label, 0) + 1
            for (tkey, dkey), text in spec.get("device_info", {}).items():
                want, _, nth = dkey.partition("#")
                if tkey.lower() in name.lower() and want.lower() == label.lower() and (not nth or int(nth) == seen[label]):
                    _set(d, "Annotation", text); log.append("Info Gerät: %s / %s" % (name, label))
    # 3) Transport + Locators
    tp = ls.find("Transport")
    _set(tp, "LoopOn", "true"); _set(tp, "LoopStart", 0); _set(tp, "LoopLength", bars); _set(tp, "CurrentTime", 0)
    locs = ls.find("Locators/Locators")
    for l in list(locs):
        locs.remove(l)
    for k, (bar, nm, note) in enumerate(spec.get("locators", [])):
        L = ET.SubElement(locs, "Locator", {"Id": str(k)})
        for tag, v in (("LomId", "0"), ("Time", str((bar - 1) * 4)), ("Name", nm), ("Annotation", note),
                       ("IsSongStart", "false")):
            ET.SubElement(L, tag).set("Value", v)
    out = spec["output"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    A.write(t, out)
    # Kontrolle: neu einlesen
    t2 = A.load(out)
    names = [A.track_name(x) for x in t2.getroot().find("LiveSet/Tracks") if x.tag != "ReturnTrack"]
    return {"output": out, "tracks": names, "log": log}
