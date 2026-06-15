# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool

# Offline audio analysis is optional: if numpy/soundfile are missing the
# server still starts, the audio tools just report that dependency clearly.
try:
    from . import audio_analysis
    _AUDIO_OK = True
    _AUDIO_IMPORT_ERROR = None
except Exception as _e:  # pragma: no cover - import guard
    audio_analysis = None
    _AUDIO_OK = False
    _AUDIO_IMPORT_ERROR = str(_e)

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None
    
    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement"
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
@telemetry_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_clip_sample_path")
def get_clip_sample_path(ctx: Context, track_index: int, clip_index: int = 0, user_prompt: str = "") -> str:
    """
    Get the on-disk sample path of an audio clip (no audio flows through the
    connection — this is the bridge to the offline analysis tools).

    For an audio clip it returns file_path plus name/length; for a MIDI clip
    file_path is null (Freeze+Flatten it to render an audio clip first).

    Parameters:
    - track_index: Index of the track holding the clip
    - clip_index: Index of the clip slot (default 0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_clip_sample_path", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip sample path: {str(e)}")
        return f"Error getting clip sample path: {str(e)}"

@mcp.tool()
@telemetry_tool("list_audio_clips")
def list_audio_clips(ctx: Context, user_prompt: str = "") -> str:
    """
    List every Session audio clip with its on-disk sample path, plus a ready
    {filename: track_index} mapping for auto_fix_masking.

    Use this to feed the masking/auto-EQ chain without writing paths or a
    mapping by hand. MIDI/instrument tracks won't appear (no sample on disk) —
    Freeze+Flatten them first if you want them included.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("list_audio_clips")
        # Build a convenience filename -> track_index mapping from the clips
        # that actually have a resolvable path.
        mapping = {}
        for c in result.get("clips", []):
            fp = c.get("file_path")
            if fp:
                mapping[os.path.basename(fp)] = c.get("track_index")
        result["filename_to_track"] = mapping
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error listing audio clips: {str(e)}")
        return f"Error listing audio clips: {str(e)}"

@mcp.tool()
@telemetry_tool("get_device_parameters")
def get_device_parameters(ctx: Context, track_index: int, device_index: int, user_prompt: str = "") -> str:
    """
    List every parameter of a device (instrument or effect) on a track.

    Use this first to discover parameter indices, names, current values and
    their legal min/max range before changing anything with
    set_device_parameter. Tip: get_track_info returns each track's devices
    with their device_index.

    Returns JSON with the device name and a parameters array, each entry:
    {index, name, value, min, max, is_quantized, display_value}
    where display_value is the human-readable form (e.g. "440 Hz", "-12.0 dB").

    Parameters:
    - track_index: Index of the track the device lives on
    - device_index: Index of the device within that track's device chain
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters from Ableton: {str(e)}")
        return f"Error getting device parameters: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    value: float,
    param_index: int | None = None,
    param_name: str | None = None,
    user_prompt: str = ""
) -> str:
    """
    Set a single parameter of a device (instrument or effect) on a track.

    Address the parameter EITHER by param_index OR by param_name (case-
    insensitive) — provide exactly one. The value is automatically clamped
    into the parameter's legal range; the response reports the value that was
    actually applied and whether it had to be clamped.

    Typical workflow:
      1. get_track_info  -> find the device_index of e.g. an "EQ Eight"
      2. get_device_parameters -> see parameter names/indices and ranges
      3. set_device_parameter  -> write the value (e.g. a frequency in Hz)

    Parameters:
    - track_index: Index of the track the device lives on
    - device_index: Index of the device within that track's device chain
    - value: The target value (in the parameter's own units; will be clamped)
    - param_index: Index of the parameter to set (use this OR param_name)
    - param_name: Name of the parameter to set, e.g. "1 Frequency A" (use this OR param_index)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        if param_index is None and param_name is None:
            return "Error: provide either param_index or param_name to identify the parameter."
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "param_index": param_index,
            "param_name": param_name,
            "value": value,
        })
        name = result.get("parameter_name", "parameter")
        applied = result.get("value")
        disp = result.get("display_value")
        clamped_note = " (clamped to legal range)" if result.get("was_clamped") else ""
        shown = f"{applied} ({disp})" if disp else f"{applied}"
        return f"Set '{name}' to {shown}{clamped_note}"
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"

@mcp.tool()
@telemetry_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"

@mcp.tool()
@telemetry_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


# ── Offline audio analysis tools ──────────────────────────────────────────
# These do NOT touch Ableton. They read rendered audio files (export or
# Freeze+Flatten a track first) so the model can "measure" what the LOM can't
# expose, then act on the result via set_device_parameter. This closes the
# loop: measure outside -> decide -> set inside.

def _check_audio_path(path: str):
    """Return an error string if the path is unusable, else None."""
    if not _AUDIO_OK:
        return (f"Audio analysis is unavailable (could not import numpy/soundfile: "
                f"{_AUDIO_IMPORT_ERROR}). Install them in the server environment.")
    if not path:
        return "An audio file path is required."
    if not os.path.isabs(path):
        return f"Path must be absolute (got: {path})."
    if not os.path.isfile(path):
        return f"File not found: {path}"
    return None


@mcp.tool()
@rich_telemetry_tool("analyze_audio")
def analyze_audio(
    ctx: Context,
    path: str,
    start_s: float | None = None,
    end_s: float | None = None,
    user_prompt: str = ""
) -> str:
    """
    Analyse a rendered audio file offline and report its spectral balance.

    Supports WAV/AIFF/FLAC directly and compressed formats (m4a/mp3) when
    ffmpeg + audioread are installed. Audio cannot be read through the Live
    connection, so export/Freeze+Flatten (or use capture_track_to_wav) first.

    Returns JSON: sample rate, duration, peak/RMS in dBFS, integrated LUFS
    (if pyloudnorm is installed), dominant frequency, spectral centroid, and
    energy per named band (sub/bass/low_mid/mid/high_mid/high).

    Parameters:
    - path: Absolute path to an audio file
    - start_s: Optional start of the section to analyse (seconds) — e.g. isolate a drop
    - end_s: Optional end of the section to analyse (seconds)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    err = _check_audio_path(path)
    if err:
        return f"Error: {err}"
    try:
        return json.dumps(audio_analysis.analyze(path, start_s=start_s, end_s=end_s), indent=2)
    except Exception as e:
        logger.error(f"Error analysing audio: {str(e)}")
        return f"Error analysing audio: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("find_frequency_masking")
def find_frequency_masking(
    ctx: Context,
    low_stem_path: str,
    high_stem_path: str,
    fmax: float = 500.0,
    user_prompt: str = ""
) -> str:
    """
    Compare two rendered stems and find where they fight in the low end
    (spectral masking) — e.g. a kick vs a bass.

    Both stems must be exported audio files (export or Freeze+Flatten first).
    Returns each source's dominant frequency, an overlap score, the most
    masked band, and a recommended high-pass frequency for the HIGH source so
    the LOW source keeps the sub. Feed the recommendation into
    set_device_parameter (e.g. an EQ Eight high-pass) and verify by ear.

    Parameters:
    - low_stem_path: Absolute path to the lower/foundation stem (e.g. the kick)
    - high_stem_path: Absolute path to the competing stem (e.g. the bass)
    - fmax: Upper frequency (Hz) of the range to compare (default 500)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    for p in (low_stem_path, high_stem_path):
        err = _check_audio_path(p)
        if err:
            return f"Error: {err}"
    try:
        return json.dumps(
            audio_analysis.find_masking(low_stem_path, high_stem_path, fmax=fmax),
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error finding frequency masking: {str(e)}")
        return f"Error finding frequency masking: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("analyze_mix")
def analyze_mix(
    ctx: Context,
    paths: List[str] | None = None,
    folder: str | None = None,
    fmax: float = 2000.0,
    user_prompt: str = ""
) -> str:
    """
    Analyse several exported stems at once and rank every masking conflict.

    Provide EITHER an explicit list of file paths in `paths`, OR a `folder`
    whose .wav/.aif/.aiff/.flac files are all analysed. (Export or
    Freeze+Flatten the tracks first — audio cannot be read live.)

    Returns JSON: per-stem level + dominant frequency, plus a conflicts list
    sorted worst-first. Each conflict names the two stems, the most masked
    band, which stem to high-pass and at what frequency. Also flags stems that
    peak near 0 dBFS (clipping risk). Work the conflicts top-down with
    set_device_parameter, then re-export and re-run to confirm.

    Parameters:
    - paths: List of absolute audio file paths (use this OR folder)
    - folder: Absolute path to a folder of stems (use this OR paths)
    - fmax: Upper frequency (Hz) of the comparison range (default 2000)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    if not _AUDIO_OK:
        return (f"Error: audio analysis unavailable (numpy/soundfile import failed: "
                f"{_AUDIO_IMPORT_ERROR}).")

    # Resolve the stem list from either argument.
    resolved = []
    if folder:
        if not os.path.isdir(folder):
            return f"Error: folder not found: {folder}"
        import glob
        exts = ("*.wav", "*.aif", "*.aiff", "*.flac", "*.WAV", "*.AIFF")
        for pat in exts:
            resolved.extend(glob.glob(os.path.join(folder, pat)))
        resolved = sorted(set(resolved))
    elif paths:
        resolved = list(paths)
    else:
        return "Error: provide either `paths` (a list of files) or `folder`."

    if len(resolved) < 2:
        return f"Error: need at least 2 stems to compare (found {len(resolved)})."

    for p in resolved:
        err = _check_audio_path(p)
        if err:
            return f"Error: {err}"

    try:
        return json.dumps(audio_analysis.analyze_mix(resolved, fmax=fmax), indent=2)
    except Exception as e:
        logger.error(f"Error analysing mix: {str(e)}")
        return f"Error analysing mix: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("extract_single_cycle")
def extract_single_cycle(
    ctx: Context,
    path: str,
    out_path: str | None = None,
    table_size: int = 2048,
    user_prompt: str = ""
) -> str:
    """
    Extract one period from a pitched sample (e.g. a bass note) and write it as
    a single-cycle WAV suitable for Ableton's Wavetable device.

    Detects the fundamental, extracts exactly one cycle, resamples it to
    table_size samples and normalises it. Returns the detected pitch and the
    output file path. NOTE: dragging the file into Wavetable to build the table
    is a manual step — the Live API cannot trigger that import.

    Parameters:
    - path: Absolute path to the source audio file (a sustained, pitched sound)
    - out_path: Where to write the single-cycle WAV (defaults to <source>_cycle.wav)
    - table_size: Samples in the output cycle, power of two recommended (default 2048)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    err = _check_audio_path(path)
    if err:
        return f"Error: {err}"
    if not out_path:
        base, _ = os.path.splitext(path)
        out_path = base + "_cycle.wav"
    if not os.path.isabs(out_path):
        return f"Error: out_path must be absolute (got: {out_path})."
    try:
        result = audio_analysis.extract_single_cycle(path, out_path, table_size=table_size)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error extracting single cycle: {str(e)}")
        return f"Error extracting single cycle: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("configure_sidechain")
def configure_sidechain(
    ctx: Context,
    bass_track_index: int,
    kick_track_index: int | None = None,
    threshold_db: float = -20.0,
    ratio: float = 4.0,
    attack_ms: float = 1.0,
    release_ms: float = 100.0,
    user_prompt: str = ""
) -> str:
    """
    Configure a Compressor on the bass track for sidechain-style ducking.

    The Live API cannot toggle a Compressor's Sidechain switch or its
    'Audio From' source, so this tool does everything else: it locates a
    Compressor already on the bass track and sets ducking-friendly Threshold /
    Ratio / Attack / Release, then returns the ONE manual step you must do in
    Live (enable Sidechain and pick the kick as the source).

    If no Compressor is on the track yet, drag one onto the bass track first,
    then run this again.

    Parameters:
    - bass_track_index: Index of the track to duck (where the Compressor lives)
    - kick_track_index: Index of the kick track to name in the manual instruction
    - threshold_db: Compressor threshold in dB (default -20)
    - ratio: Compression ratio (default 4.0)
    - attack_ms: Attack time in ms (default 1.0 — fast, for tight ducking)
    - release_ms: Release time in ms (default 100)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        info = ableton.send_command("get_track_info", {"track_index": bass_track_index})

        comp_index = None
        for d in info.get("devices", []):
            label = (str(d.get("name", "")) + " " + str(d.get("class_name", ""))).lower()
            if "compressor" in label:
                comp_index = d.get("index")
                break
        if comp_index is None:
            return (f"No Compressor found on track {bass_track_index} "
                    f"('{info.get('name', '?')}'). Drag a Compressor onto that track, "
                    f"then run configure_sidechain again.")

        targets = [
            ("Threshold", threshold_db),
            ("Ratio", ratio),
            ("Attack", attack_ms),
            ("Release", release_ms),
        ]
        applied, skipped = [], []
        for name, value in targets:
            try:
                res = ableton.send_command("set_device_parameter", {
                    "track_index": bass_track_index,
                    "device_index": comp_index,
                    "param_name": name,
                    "value": value,
                })
                applied.append(f"{res.get('parameter_name', name)} -> {res.get('display_value') or res.get('value')}")
            except Exception as pe:
                skipped.append(f"{name} ({pe})")

        kick_hint = f"track {kick_track_index}" if kick_track_index is not None else "your kick track"
        lines = [
            f"Configured Compressor (device {comp_index}) on track {bass_track_index}.",
            "Set: " + (", ".join(applied) if applied else "nothing"),
        ]
        if skipped:
            lines.append("Could not set (name not found on this device): " + ", ".join(skipped))
        lines.append(
            f"MANUAL STEP (Live API limitation): in that Compressor, enable "
            f"'Sidechain' and set 'Audio From' to {kick_hint}. That single toggle "
            f"is the only part the API can't do.")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error configuring sidechain: {str(e)}")
        return f"Error configuring sidechain: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("auto_fix_masking")
def auto_fix_masking(
    ctx: Context,
    mapping: Dict[str, int] | None = None,
    paths: List[str] | None = None,
    folder: str | None = None,
    fmax: float = 2000.0,
    max_fixes: int = 3,
    apply: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Close the loop autonomously: analyse stems, then high-pass the masking
    stems with EQ Eight.

    If you pass NOTHING, it pulls the Session's audio clips straight from Live
    (via list_audio_clips), using their on-disk sample paths as the stems and
    auto-building the {filename: track_index} mapping. You can also pass `paths`
    or `folder` explicitly, and/or an explicit `mapping`.

    Runs analyze_mix, then for the worst conflicts finds an EQ Eight on the
    mapped track and sets its low band (Band 1) as a high-pass at the
    recommended frequency.

    By default this is a DRY RUN: it returns the exact plan without changing
    anything. Pass apply=true to actually write the parameters. After applying,
    re-export and re-run analyze_mix to confirm — and always check by ear.

    Parameters:
    - mapping: Optional {stem filename: track_index}. Auto-built from Live if omitted.
    - paths: Optional list of absolute stem paths. Pulled from Live if omitted.
    - folder: Optional folder of stems (use this OR paths)
    - fmax: Upper frequency (Hz) of the comparison range (default 2000)
    - max_fixes: Maximum number of conflicts to address, worst-first (default 3)
    - apply: If true, write the EQ changes; if false (default), only return the plan
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    if not _AUDIO_OK:
        return (f"Error: audio analysis unavailable (numpy/soundfile import failed: "
                f"{_AUDIO_IMPORT_ERROR}).")

    # Resolve stems. If neither paths nor folder is given, pull the Session's
    # audio clips from Live and (when no mapping was supplied) derive one.
    resolved = []
    if folder:
        if not os.path.isdir(folder):
            return f"Error: folder not found: {folder}"
        import glob
        for pat in ("*.wav", "*.aif", "*.aiff", "*.flac", "*.WAV", "*.AIFF"):
            resolved.extend(glob.glob(os.path.join(folder, pat)))
        resolved = sorted(set(resolved))
    elif paths:
        resolved = list(paths)
    else:
        try:
            clips = get_ableton_connection().send_command("list_audio_clips")
        except Exception as e:
            return (f"Error: no paths/folder given and could not pull audio clips "
                    f"from Live ({e}). Provide `paths` or `folder` instead.")
        with_path = [c for c in clips.get("clips", []) if c.get("file_path")]
        resolved = [c["file_path"] for c in with_path]
        if mapping is None:
            mapping = {os.path.basename(c["file_path"]): c["track_index"] for c in with_path}

    if len(resolved) < 2:
        return f"Error: need at least 2 stems (found {len(resolved)})."
    for p in resolved:
        e = _check_audio_path(p)
        if e:
            return f"Error: {e}"

    # If a stem list was given without a mapping, try to derive one from Live.
    if mapping is None:
        try:
            clips = get_ableton_connection().send_command("list_audio_clips")
            mapping = {os.path.basename(c["file_path"]): c["track_index"]
                       for c in clips.get("clips", []) if c.get("file_path")}
        except Exception:
            mapping = {}

    try:
        report = audio_analysis.analyze_mix(resolved, fmax=fmax)
    except Exception as e:
        logger.error(f"Error analysing mix: {str(e)}")
        return f"Error analysing mix: {str(e)}"

    ableton = get_ableton_connection()
    plan = []
    for conflict in report.get("conflicts", [])[:max_fixes]:
        if conflict.get("overlap_score", 0) <= 0:
            continue
        stem = conflict["highpass_target"]
        freq = conflict["recommended_highpass_hz"]
        track_index = mapping.get(stem)
        step = {"stem": stem, "vs": conflict["stem_a"] if conflict["stem_b"] == stem else conflict["stem_b"],
                "highpass_hz": freq, "track_index": track_index, "status": "planned"}

        if track_index is None:
            step["status"] = "skipped: no track mapping for this stem"
            plan.append(step)
            continue

        # Find an EQ Eight on the track.
        try:
            info = ableton.send_command("get_track_info", {"track_index": track_index})
        except Exception as te:
            step["status"] = f"skipped: get_track_info failed ({te})"
            plan.append(step)
            continue

        eq_index = None
        for d in info.get("devices", []):
            label = (str(d.get("name", "")) + " " + str(d.get("class_name", ""))).lower()
            if "eq eight" in label or "eq8" in label:
                eq_index = d.get("index")
                break
        if eq_index is None:
            step["status"] = f"skipped: no EQ Eight on track {track_index} (add one)"
            plan.append(step)
            continue
        step["device_index"] = eq_index

        # Locate Band 1 frequency + enable params by name.
        try:
            params = ableton.send_command("get_device_parameters",
                                          {"track_index": track_index, "device_index": eq_index})
        except Exception as pe:
            step["status"] = f"skipped: get_device_parameters failed ({pe})"
            plan.append(step)
            continue

        freq_param = next((p["name"] for p in params.get("parameters", [])
                           if p["name"].lower() in ("1 frequency a", "1 frequency")), None)
        if freq_param is None:
            freq_param = next((p["name"] for p in params.get("parameters", [])
                               if "frequency" in p["name"].lower() and p["name"].strip().startswith("1")), None)
        on_param = next((p["name"] for p in params.get("parameters", [])
                         if "filter on a" in p["name"].lower() or p["name"].lower() in ("1 filter on a",)), None)

        if freq_param is None:
            step["status"] = "skipped: could not find Band 1 frequency parameter"
            plan.append(step)
            continue
        step["freq_param"] = freq_param
        step["enable_param"] = on_param

        if not apply:
            step["status"] = "dry-run (set apply=true to write)"
            plan.append(step)
            continue

        # Apply: enable band 1 and set its frequency. Filter SHAPE (high-pass)
        # must be a high-pass type — verify in Live; we set freq + enable here.
        try:
            if on_param:
                ableton.send_command("set_device_parameter", {
                    "track_index": track_index, "device_index": eq_index,
                    "param_name": on_param, "value": 1})
            res = ableton.send_command("set_device_parameter", {
                "track_index": track_index, "device_index": eq_index,
                "param_name": freq_param, "value": freq})
            step["status"] = f"applied: {freq_param} -> {res.get('display_value') or res.get('value')}"
        except Exception as ae:
            step["status"] = f"error applying ({ae})"
        plan.append(step)

    return json.dumps({
        "mode": "applied" if apply else "dry-run",
        "top_conflict": report.get("top_conflict"),
        "warnings": report.get("warnings", []),
        "fixes": plan,
        "note": ("Ensure EQ Eight Band 1 is set to a High-Pass shape; this tool sets "
                 "the frequency and enables the band but the filter TYPE should be "
                 "verified in Live. Re-export and re-run analyze_mix to confirm."),
    }, indent=2)


@mcp.tool()
@rich_telemetry_tool("delete_device")
def delete_device(ctx: Context, track_index: int, device_index: int, user_prompt: str = "") -> str:
    """
    Delete a device from a track's chain.

    IMPORTANT: the Live API can NOT move/reorder devices — only delete. So
    there is no lossless auto-reorder: to change order you'd delete and reload,
    losing all settings (especially on VST/AU). Prefer loading devices in the
    right order in the first place (new devices append to the end of the
    chain), or drag to reorder by hand in Live. Use this only when you really
    want a device gone.

    Parameters:
    - track_index: Index of the track
    - device_index: Index of the device to delete within that track's chain
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_device", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return f"Deleted '{result.get('deleted', '?')}'. Remaining devices: {result.get('remaining_devices')}"
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("capture_track_to_wav")
def capture_track_to_wav(
    ctx: Context,
    source_track_index: int,
    length_seconds: float = 8.0,
    user_prompt: str = ""
) -> str:
    """
    Render a track (with its full device chain) to audio via REAL-TIME
    resampling, so you can measure the processed result — the before/after
    proof offline analysis needs.

    The Live API has no offline bounce, so this works by: soloing the source
    track, recording the master into a dedicated 'MCP Capture' audio track in
    real time for length_seconds, then stopping. Afterwards the captured clip's
    path is returned (feed it to analyze_audio / analyze_mix).

    CAVEATS (honest): runs in real time (not instant); a freshly recorded clip
    may report an empty file_path until the Set is saved — if so, save the Set
    and call get_clip_sample_path on the capture track. If this proves fiddly,
    a manual File > Export Audio is the pragmatic alternative.

    Parameters:
    - source_track_index: Index of the track to capture (it is soloed during capture)
    - length_seconds: How long to record in real time (default 8, max 600)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        import time
        ableton = get_ableton_connection()
        start = ableton.send_command("start_resample_capture", {
            "source_track_index": source_track_index,
        })
        cap_index = start.get("capture_track_index")
        time.sleep(max(0.5, min(float(length_seconds), 600.0)))
        stop = ableton.send_command("stop_resample_capture", {
            "capture_track_index": cap_index,
            "source_track_index": source_track_index,
        })
        return json.dumps({
            "start": start,
            "stop": stop,
            "next": ("If file_path is null, save the Set (Cmd+S) then call "
                     "get_clip_sample_path(track_index=%s, clip_index=0)." % cap_index),
        }, indent=2)
    except Exception as e:
        logger.error(f"Error capturing track: {str(e)}")
        return f"Error capturing track: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()