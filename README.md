# AbletonMCP — Extended Fork (Measure · Edit · Render · Mix · Auto-fix)

> **This is a fork of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp)** (MIT, © Siddharth Ahuja).
> It keeps everything the original does and extends it to
> **64 tools**. All credit for the original architecture
> goes to the upstream project — please ⭐ it.

AbletonMCP connects Ableton Live to an MCP client (Claude Desktop / Claude Code / Cursor)
so the model can control a live set. The stock version can **build and trigger**
things (tracks, clips, notes, load devices). This fork lets the model work like an
engineer in the session: **arrange, route, automate, render, measure against
references and self-correct** — closing the loop *measure → change → re-measure*.

Built and used on real productions (techno / house), not just demos.

---

## What this fork adds

| Area | Tools |
|---|---|
| **Device parameters** | `get_device_parameters`, `set_device_parameter`, `delete_device` |
| **Racks** | `get_rack`, `get_nested_device_parameters`, `set_nested_device_parameter`, `set_rack_chain` — reach devices inside Audio/Instrument/Drum Racks |
| **Mixer** | `set_mixer` (fader, pan, sends, mute, solo — tracks, returns, master), `get_meters` (live output meters of every track while playing) |
| **Routing & sidechain** | `get_routing` / `set_routing` (Audio/MIDI From/To by name), `get_device_io` / `set_device_io` (e.g. Compressor sidechain source), `configure_sidechain` |
| **Tracks** | `create_audio_track`, `delete_track` |
| **Arrangement** | `get_arrangement_clips`, `set_arrangement_clip`, `copy_arrangement_clip`, `delete_arrangement_clips`, `duplicate_to_arrangement`, `set_song_loop`, `set_locators` |
| **MIDI editing** | `get_clip_notes`, `transpose_clip` (session or arrangement clips) |
| **Automation** | `add_clip_automation`, `clear_clip_automation` — write device-parameter envelopes into clips |
| **Samples** | `load_sample` (by browser name), `get_clip_sample_path`, `list_audio_clips`; m4a/mp3 support via `audioread` + `ffmpeg` |
| **Render (no export dialog)** | `render_master`, `capture_section`, `capture_track_to_wav`, `render_and_measure` — real-time recording of master, bar ranges or single tracks |
| **Analysis** | `analyze_audio` (peak/RMS, bands, **LUFS**, time windows), `find_frequency_masking`, `analyze_mix`, `measure_in_mix` (what a track really adds to the full mix) |
| **Reference check** | `reference_check` — your render vs. pro reference tracks on the loudest 30 s (the drop): loudness, bass weight, kick punch, low-mids, mids, highs, density (PLR), true peak, stereo in the bass — traffic light per criterion |
| **Vocal fit** | `fit_vocal` — detects key/tuning/tempo of an acapella (YIN pitch track, pure numpy/scipy) and fits it to the song: transpose, fine-tune, warp |
| **Auto-fix** | `auto_fix_masking` — pulls clips + mapping from Live, sets EQ-Eight high-pass against masking (dry run first) |
| **Live Set file editor** | `als_info`, `als_edit` — edits the `.als` file directly for things the Live API can't do (grouping, reordering tracks, sidechain sources, routing). Never touches the original: works on a `<Name> MCP.als` copy with backups, then reopens it |
| **Session** | `save_set` (Cmd+S via macOS Accessibility) |
| **Generate** | `extract_single_cycle` (single-cycle wavetable WAV) |
| **Stability** | reads run thread-safely on Live's main thread with a fresh song handle (fixes the upstream `TPyHandle<ASong>` error) |

### Extras outside the MCP server

- **`tools/`** — standalone helpers: `wavetable_gen.py` (wavetables for Ableton Wavetable or Serum/Vital), `fx_generator.py` (tempo-synced, key-tuned FX starter pack), `fx_index.py` (catalogue an FX library by length, key, loudness, type), `make_template.py` (teaching template from a finished set), `bass_fingerprint.py`, `live.py` (direct socket client for scripting).
- **`m4l/`** — `build_lek.py` builds two Max for Live devices, **LEK Kick** + **LEK Bass** ("low-end coupling"): the bass ducks only its sub band exactly on the kick grid, with lookahead, read from the kick clips in the set.

### Honest API limitations (not bugs — unavailable in the Live Object Model)
- No offline render/bounce/freeze → all renders are **real-time** recordings.
- No device **reorder** via the API → load devices in order, or use `als_edit` on the file.
- Grouping tracks isn't exposed by the API → `als_edit`.
- **Wavetable** custom-file import can't be triggered (drag it in manually).
- VST/AU **"Configure"** can't be clicked to expose parameters.

---

## Requirements
- Ableton **Live 11+** (developed against 11.3.x)
- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (`brew install uv` on macOS)
- **ffmpeg** on PATH (only needed for m4a/mp3 analysis): `brew install ffmpeg`

Python deps (resolved automatically by uv from `pyproject.toml`):
`mcp[cli]`, `numpy`, `soundfile`, `audioread`, `pyloudnorm` (pulls `scipy`).

---

## Install

### 1. Remote Script (inside Ableton)
Copy `AbletonMCP_Remote_Script/__init__.py` into a folder named **`AbletonMCP`**
in Ableton's user remote-scripts location, then fully restart Live:

- **macOS (reliable):** `~/Music/Ableton/User Library/Remote Scripts/AbletonMCP/__init__.py`
- **Windows:** `%USERPROFILE%\Documents\Ableton\User Library\Remote Scripts\AbletonMCP\__init__.py`

Then in Live: **Settings → Link, Tempo & MIDI → Control Surface → AbletonMCP**, Input/Output = **None**.

### 2. MCP server (Claude Desktop)
Edit `claude_desktop_config.json`. To run **this local fork** (so your changes
take effect), point uv at the repo:

```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "uv",
      "args": ["run", "--directory", "/ABSOLUTE/PATH/TO/ableton-mcp", "ableton-mcp"]
    }
  }
}
```

(Use the absolute path to wherever you cloned this repo. On macOS, `command`
may need the full uv path, e.g. `/opt/homebrew/bin/uv`.)

Restart Claude Desktop — you should see the AbletonMCP tools (hammer icon).

---

## Typical workflows

**Mix check against references**
1. `render_master` (or `render_and_measure` for a bar range).
2. `reference_check` with 1–3 pro tracks → traffic light per criterion.
3. Fix with `set_mixer`, `set_device_parameter`, `set_nested_device_parameter`, re-render, re-check.

**Masking**
1. `analyze_mix` (no args) → pulls clips + mapping from Live, ranks masking conflicts.
2. `auto_fix_masking` (dry run) → review → run again with `apply=true`.
3. `measure_in_mix` to confirm what the track now adds to the full mix.

**Vocal into a track**
1. `load_sample` the acapella → `fit_vocal` → key, tuning and tempo matched.

Always trust your ears — the numbers are starting points.

---

## Privacy / Telemetry
The upstream project includes optional anonymous telemetry. **In this fork it is
off by default:** consent defaults to `False`, and no `MCP_Server/config.py`
(which held the backend credentials) is shipped, so nothing can be sent. It only
activates if you add your own config and set `ABLETON_MCP_TELEMETRY_CONSENT=true`.
Hard-disable anytime with `DISABLE_TELEMETRY=true`.

---

## License & attribution
MIT — see [LICENSE](LICENSE). Original work © 2025 Siddharth Ahuja
([ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp)). This fork
preserves the original license and adds the features listed above.
