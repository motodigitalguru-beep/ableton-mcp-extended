# AbletonMCP — Extended Fork (Measure · Set · Generate · Auto-fix)

> **This is a fork of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp)** (MIT, © Siddharth Ahuja).
> It keeps everything the original does and adds device-parameter read/write,
> offline audio analysis, single-cycle wavetable export, and an autonomous
> anti-masking chain. All credit for the original architecture goes to the
> upstream project — please ⭐ it.

AbletonMCP connects Ableton Live to an MCP client (Claude Desktop / Cursor) so
the model can control a live set. The stock version can **build and trigger**
things (tracks, clips, notes, load devices). This fork additionally lets the
model **hear, set, generate and self-correct** — closing the loop
*measure → set → re-measure*.

---

## What this fork adds

| Area | Stock | This fork |
|---|---|---|
| Device parameters | load only | **read** (`get_device_parameters`) + **write** (`set_device_parameter`) + **delete** (`delete_device`) |
| Audio analysis | — | `analyze_audio` (peak/RMS, bands, **LUFS**, **time-window**), `find_frequency_masking`, `analyze_mix` |
| File formats | WAV/AIFF/FLAC | **+ m4a/mp3** (via `audioread` + `ffmpeg`) |
| Generate | — | `extract_single_cycle` (single-cycle wavetable WAV) |
| Audio→file bridge | — | `get_clip_sample_path`, `list_audio_clips` |
| Mix automation | — | `auto_fix_masking` — pulls clips + mapping straight from Live, sets EQ-Eight high-pass against masking |
| Sidechain | — | `configure_sidechain` (sets compressor values; the sidechain *switch* stays manual — Live API limit) |
| Render/capture | — | `capture_track_to_wav` (real-time resampling) |
| Stability | `get_track_info` could throw a C++ `TPyHandle<ASong>` error | reads run thread-safely on Live's main thread + fresh song handle |

### ⚠️ Experimental (not yet confirmed in a live Ableton session)
`capture_track_to_wav`, `delete_device`, and the `get_track_info` thread-safety
fix are implemented and import-clean, but the live-operation behaviour still
needs real-world testing. The offline analysis/generation tools are verified.

### Honest API limitations (not bugs — unavailable in the Live Object Model)
- No offline render/bounce/freeze → `capture_track_to_wav` uses **real-time** resampling; manual `File > Export Audio` remains the pragmatic fallback.
- No device **reorder** (only delete) → load devices in the order you want (they append to the chain end).
- Compressor **Sidechain** switch + "Audio From" source can't be set.
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

## Typical workflow
1. Have audio clips in the set (or Freeze+Flatten instrument tracks first).
2. `analyze_mix` (no args) → pulls clips + builds the mapping from Live, ranks masking conflicts.
3. `auto_fix_masking` (dry run) → review the plan → run again with `apply=true`.
4. `capture_track_to_wav` or manual export → `analyze_mix` again to verify.
5. Always trust your ears — the recommendations are starting points.

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
