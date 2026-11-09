# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import os
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
HOST = "0.0.0.0"

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP Remote Script initializing...")
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()
        
        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Route the command to the appropriate handler.
            # NOTE: reads are scheduled on the main thread too. Touching the
            # Live Object Model from the socket thread can intermittently raise
            # boost.python signature errors (e.g. TPyHandle<ASong>), so every
            # LOM access goes through _run_on_main_thread.
            if command_type == "get_session_info":
                response["result"] = self._run_on_main_thread(self._get_session_info)
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                track_type = params.get("track_type", "track")
                response["result"] = self._run_on_main_thread(
                    lambda: self._get_track_info(track_index, track_type))
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in ["create_midi_track", "set_track_name",
                                 "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
                                 "set_tempo", "fire_clip", "stop_clip",
                                 "start_playback", "stop_playback", "load_browser_item",
                                 # Arrangement view – must run on the main thread
                                 "switch_to_arrangement_view", "set_current_song_time",
                                 "duplicate_session_clip_to_arrangement",
                                 # Device parameter write – must run on the main thread
                                 "set_device_parameter",
                                 # Device chain + capture – must run on the main thread
                                 "delete_device", "create_audio_track",
                                 "start_resample_capture", "stop_resample_capture",
                                 # Session-Aufräumen + Warp (2026-09-21)
                                 "delete_track", "delete_clip", "set_clip_warp", "set_track_volume", "get_clip_info", "get_clip_notes",
                                 "add_clip_automation", "clear_clip_automation", "set_arrangement_clip", "set_song_loop", "delete_arrangement_clips", "duplicate_clip_to_slot", "transpose_clip", "remove_clip_notes", "set_warp_markers", "get_warp_markers",
                                 # fit_vocal (2026-10-14)
                                 "clip_props", "get_song_scale", "get_cue_points", "cue_point",
                                 # Racks + Hot-Reload (2026-10-17)
                                 "hot_reload", "insert_device", "rack_add_sample_chain", "rack_info", "rack_select_chain", "set_fold", "set_clip_notes", "set_song_scale", "create_arrangement_midi_clip", "rack_insert_chain", "create_return_track", "create_arrangement_audio_clip",
                                 # Mixer (2026-10-14)
                                 "set_mixer", "transport_state", "arrangement_record",
                                 # Ausbau Mess-/Rack-/Routing-/Clip-Werkzeuge (2026-10-14)
                                 "get_meters", "get_rack", "get_nested_device_parameters",
                                 "set_nested_device_parameter", "set_rack_chain",
                                 "get_device_io", "set_device_io",
                                 "get_routing", "set_routing",
                                 "copy_arrangement_clip", "load_sample_file"]:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            if params.get("track_type", "track") != "track":
                                tr = self._tracks_of(params["track_type"])[int(track_index)]
                                tr.name = name
                                result = {"name": tr.name}
                            else:
                                result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "create_audio_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            path = params.get("path", "")
                            result = self._create_audio_clip(track_index, clip_index, path)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "load_instrument_or_effect":
                            track_index = params.get("track_index", 0)
                            uri = params.get("uri", "")
                            result = self._load_instrument_or_effect(track_index, uri)
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            result = self._load_browser_item(track_index, item_uri,
                                                             params.get("track_type", "track"))
                        elif command_type == "set_mixer":
                            result = self._set_mixer(params)
                        elif command_type == "transport_state":
                            result = self._transport_state(params)
                        elif command_type == "arrangement_record":
                            result = self._arrangement_record(params)
                        elif command_type == "get_meters":
                            result = self._get_meters()
                        elif command_type == "get_rack":
                            result = self._get_rack(params)
                        elif command_type == "get_nested_device_parameters":
                            result = self._get_nested_device_parameters(params)
                        elif command_type == "set_nested_device_parameter":
                            result = self._set_nested_device_parameter(params)
                        elif command_type == "set_rack_chain":
                            result = self._set_rack_chain(params)
                        elif command_type == "get_device_io":
                            result = self._get_device_io(params)
                        elif command_type == "set_device_io":
                            result = self._set_device_io(params)
                        elif command_type == "get_routing":
                            result = self._get_routing(params)
                        elif command_type == "set_routing":
                            result = self._set_routing(params)
                        elif command_type == "copy_arrangement_clip":
                            result = self._copy_arrangement_clip(params)
                        elif command_type == "load_sample_file":
                            result = self._load_sample_file(params)
                        # ── Arrangement view commands ──────────────────────────────
                        elif command_type == "switch_to_arrangement_view":
                            result = self._switch_to_arrangement_view()
                        elif command_type == "set_current_song_time":
                            time_val = params.get("time", 0.0)
                            result = self._set_current_song_time(time_val)
                        elif command_type == "duplicate_session_clip_to_arrangement":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            destination_time = params.get("destination_time", 0.0)
                            result = self._duplicate_session_clip_to_arrangement(
                                track_index, clip_index, destination_time)
                        elif command_type == "set_device_parameter":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            param_index = params.get("param_index", None)
                            param_name = params.get("param_name", None)
                            value = params.get("value", 0.0)
                            track_type = params.get("track_type", "track")
                            result = self._set_device_parameter(
                                track_index, device_index, value,
                                param_index=param_index, param_name=param_name,
                                track_type=track_type)
                        elif command_type == "delete_device":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            result = self._delete_device(track_index, device_index)
                        elif command_type == "delete_track":
                            result = self._delete_track(params.get("track_index", 0))
                        elif command_type == "delete_clip":
                            result = self._delete_clip(params.get("track_index", 0), params.get("clip_index", 0))
                        elif command_type == "set_clip_warp":
                            result = self._set_clip_warp(params.get("track_index", 0), params.get("clip_index", 0),
                                                         params.get("warping", None), params.get("warp_mode", None),
                                                         params.get("loop_start", None), params.get("loop_end", None),
                                                         params.get("start_marker", None), params.get("looping", None))
                        elif command_type == "set_track_volume":
                            result = self._set_track_volume(params.get("track_index", 0), params.get("volume", 0.85))
                        elif command_type == "delete_arrangement_clips":
                            result = self._delete_arrangement_clips(params.get("track_index", 0), params.get("start", None), params.get("end", None))
                        elif command_type == "duplicate_clip_to_slot":
                            result = self._duplicate_clip_to_slot(params.get("track_index", 0), params.get("clip_index", 0), params.get("target_index", 0))
                        elif command_type == "add_clip_automation":
                            result = self._add_clip_automation(params)
                        elif command_type == "clear_clip_automation":
                            result = self._clear_clip_automation(params)
                        elif command_type == "set_arrangement_clip":
                            result = self._set_arrangement_clip(params)
                        elif command_type == "set_song_loop":
                            result = self._set_song_loop(params)
                        elif command_type == "get_clip_notes":
                            result = self._get_clip_notes(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
                        elif command_type == "set_warp_markers":
                            result = self._set_warp_markers(params)
                        elif command_type == "get_warp_markers":
                            result = self._get_warp_markers(params)
                        elif command_type == "clip_props":
                            result = self._clip_props(params)
                        elif command_type == "get_song_scale":
                            result = self._get_song_scale()
                        elif command_type == "get_cue_points":
                            result = self._get_cue_points()
                        elif command_type == "cue_point":
                            result = self._cue_point(params)
                        elif command_type == "hot_reload":
                            result = self._hot_reload()
                        elif command_type == "set_fold":
                            result = self._set_fold(params)
                        elif command_type == "set_clip_notes":
                            result = self._set_clip_notes(params)
                        elif command_type == "create_arrangement_audio_clip":
                            result = self._create_arrangement_audio_clip(params)
                        elif command_type == "create_return_track":
                            result = self._create_return_track(params)
                        elif command_type == "rack_insert_chain":
                            result = self._rack_insert_chain(params)
                        elif command_type == "create_arrangement_midi_clip":
                            result = self._create_arrangement_midi_clip(params)
                        elif command_type == "set_song_scale":
                            result = self._set_song_scale(params)
                        elif command_type == "insert_device":
                            result = self._insert_device(params)
                        elif command_type == "rack_add_sample_chain":
                            result = self._rack_add_sample_chain(params)
                        elif command_type == "rack_info":
                            result = self._rack_info(params)
                        elif command_type == "rack_select_chain":
                            result = self._rack_select_chain(params)
                        elif command_type == "transpose_clip":
                            result = self._transpose_clip(params)
                        elif command_type == "remove_clip_notes":
                            result = self._remove_clip_notes(params)
                        elif command_type == "get_clip_info":
                            result = self._get_clip_info(params.get("track_index", 0), params.get("clip_index", 0))
                        elif command_type == "create_audio_track":
                            index = params.get("index", -1)
                            result = self._create_audio_track(index)
                        elif command_type == "start_resample_capture":
                            source_index = params.get("source_track_index", None)
                            result = self._start_resample_capture(source_index,
                                                                  params.get("start_time", None),
                                                                  params.get("mute_tracks", None))
                        elif command_type == "stop_resample_capture":
                            capture_index = params.get("capture_track_index", 0)
                            source_index = params.get("source_track_index", None)
                            result = self._stop_resample_capture(capture_index, source_index)

                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # Wait for the response with a timeout. Some commands (notably
                # create_audio_clip, which decodes/imports the audio file on
                # the main thread) can take longer than the default 10s on
                # larger files — give them more headroom.
                long_running_commands = {"create_audio_clip": 60.0, "rack_add_sample_chain": 30.0, "hot_reload": 30.0}
                queue_timeout = long_running_commands.get(command_type, 10.0)
                try:
                    task_response = response_queue.get(timeout=queue_timeout)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                response["result"] = self.get_browser_items_at_path(path)
            elif command_type == "get_arrangement_clips":
                track_index = params.get("track_index", 0)
                response["result"] = self._run_on_main_thread(
                    lambda: self._get_arrangement_clips(track_index))
            elif command_type == "get_device_parameters":
                track_index = params.get("track_index", 0)
                device_index = params.get("device_index", 0)
                track_type = params.get("track_type", "track")
                response["result"] = self._run_on_main_thread(
                    lambda: self._get_device_parameters(track_index, device_index, track_type))
            elif command_type == "get_clip_sample_path":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                response["result"] = self._run_on_main_thread(
                    lambda: self._get_clip_sample_path(track_index, clip_index))
            elif command_type == "list_audio_clips":
                response["result"] = self._run_on_main_thread(self._list_audio_clips)
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Command implementations
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Transport / playback state — lets clients render a live
                # playhead without polling separately. Each property is read
                # via _safe_song_property so an attribute missing on a given
                # Live version falls back to its default rather than breaking
                # the response shape.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _tracks_of(self, track_type="track"):
        """Track collection by type: 'track' (default), 'return' or 'master'."""
        t = str(track_type or "track").lower()
        if t == "return":
            return list(self._song.return_tracks)
        if t == "master":
            return [self._song.master_track]
        return list(self._song.tracks)

    def _get_track_info(self, track_index, track_type="track"):
        """Get information about a track (track_type: track | return | master)"""
        try:
            tracks = self._tracks_of(track_type)
            if track_index < 0 or track_index >= len(tracks):
                raise IndexError(
                    "track_index %d out of range (valid 0..%d). Return tracks "
                    "(%d) and the master track are separate collections, not "
                    "part of the track index." % (
                        track_index, len(tracks) - 1, len(self._song.return_tracks)))

            track = tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(getattr(track, "clip_slots", [])):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            result = {
                "index": track_index,
                "name": track.name,
                "track_type": str(track_type or "track").lower(),
                "is_audio_track": getattr(track, "has_audio_input", False),
                "is_midi_track": getattr(track, "has_midi_input", False),
                "mute": self._safe_attr(track, "mute", False),
                "solo": self._safe_attr(track, "solo", False),
                # Gruppen-, Return- und Master-Spuren haben kein Arm: der Zugriff
                # wirft eine Exception statt den getattr-Default zu liefern.
                "arm": self._safe_attr(track, "arm", False),
                "is_group": bool(self._safe_attr(track, "is_foldable", False)),
                "group": self._safe_group_name(track),
                "sends": [sd.value for sd in track.mixer_device.sends] if hasattr(track.mixer_device, "sends") else [],
                "sends_db": [self._param_str(sd) for sd in track.mixer_device.sends] if hasattr(track.mixer_device, "sends") else [],
                "volume": track.mixer_device.volume.value,
                "volume_db": self._param_str(track.mixer_device.volume),
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise

    def _get_device_parameters(self, track_index, device_index, track_type="track"):
        """List every parameter of a device on a track (read-only).

        Returns each parameter's index, name, current value, legal range,
        whether it is quantized, and a human-readable display value
        (e.g. "440 Hz", "-12.0 dB") so a caller can map musical intent to
        raw values before writing them back via set_device_parameter.
        """
        try:
            tracks = self._tracks_of(track_type)
            if track_index < 0 or track_index >= len(tracks):
                raise IndexError("Track index out of range")
            track = tracks[track_index]

            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]

            parameters = []
            for i, p in enumerate(device.parameters):
                display = None
                try:
                    display = p.str_for_value(p.value)
                except Exception:
                    pass
                parameters.append({
                    "index": i,
                    "name": p.name,
                    "value": p.value,
                    "min": p.min,
                    "max": p.max,
                    "is_quantized": p.is_quantized,
                    "display_value": display,
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "device_index": device_index,
                "device_name": device.name,
                "class_name": device.class_name,
                "parameter_count": len(parameters),
                "parameters": parameters,
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, value,
                              param_index=None, param_name=None, track_type="track"):
        """Set a single device parameter, addressed by index OR name.

        The value is clamped into the parameter's legal [min, max] range
        because the Live API raises if a value is out of range. Returns the
        actually-applied value plus a 'clamped' flag so the caller knows if
        its request was adjusted.
        """
        try:
            tracks = self._tracks_of(track_type)
            if track_index < 0 or track_index >= len(tracks):
                raise IndexError("Track index out of range")
            track = tracks[track_index]

            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]

            # Resolve the target parameter by index or case-insensitive name
            param = None
            if param_index is not None:
                if param_index < 0 or param_index >= len(device.parameters):
                    raise IndexError("Parameter index out of range")
                param = device.parameters[param_index]
            elif param_name is not None:
                for p in device.parameters:
                    if p.name.lower() == str(param_name).lower():
                        param = p
                        break
                if param is None:
                    raise ValueError(
                        "Parameter '%s' not found on device '%s'" % (param_name, device.name))
            else:
                raise ValueError("Either param_index or param_name is required")

            if not param.is_enabled:
                raise Exception("Parameter '%s' is not automatable" % param.name)

            requested = float(value)
            clamped_value = max(param.min, min(param.max, requested))
            param.value = clamped_value

            display = None
            try:
                display = param.str_for_value(param.value)
            except Exception:
                pass

            return {
                "track_index": track_index,
                "device_index": device_index,
                "device_name": device.name,
                "parameter_name": param.name,
                "requested_value": requested,
                "value": param.value,
                "was_clamped": clamped_value != requested,
                "min": param.min,
                "max": param.max,
                "display_value": display,
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _run_on_main_thread(self, fn, timeout=10.0):
        """Execute ``fn`` on Live's main thread and return its result.

        All Live Object Model access should go through here: touching the LOM
        from the socket thread can raise intermittent boost.python signature
        errors. Raises if ``fn`` raises or the main thread doesn't answer in
        time.
        """
        result_queue = queue.Queue()

        def task():
            try:
                # Refresh the cached song handle on the main thread. A handle
                # cached at __init__ can go stale after a new Set is loaded and
                # then raise boost.python TPyHandle<ASong> errors on access.
                try:
                    self._song = self.song()
                except Exception:
                    pass
                result_queue.put({"ok": True, "result": fn()})
            except Exception as e:
                self.log_message("Main-thread read error: " + str(e))
                self.log_message(traceback.format_exc())
                result_queue.put({"ok": False, "error": str(e)})

        try:
            self.schedule_message(0, task)
        except AssertionError:
            # Already on the main thread – run directly.
            task()

        try:
            outcome = result_queue.get(timeout=timeout)
        except queue.Empty:
            raise Exception("Timeout waiting for main-thread operation")

        if not outcome["ok"]:
            raise Exception(outcome["error"])
        return outcome["result"]

    def _get_clip_sample_path(self, track_index, clip_index):
        """Return the on-disk sample path (and metadata) for an audio clip."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if clip_index < 0 or clip_index >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        slot = track.clip_slots[clip_index]
        if not slot.has_clip:
            raise Exception("No clip in track %d slot %d" % (track_index, clip_index))

        clip = slot.clip
        is_audio = clip.is_audio_clip
        file_path = None
        if is_audio:
            try:
                file_path = clip.file_path
            except Exception:
                file_path = None

        return {
            "track_index": track_index,
            "track_name": track.name,
            "clip_index": clip_index,
            "name": clip.name,
            "is_audio_clip": is_audio,
            "is_midi_clip": clip.is_midi_clip,
            "length": clip.length,
            "file_path": file_path,
        }

    def _list_audio_clips(self):
        """List every Session audio clip with its on-disk sample path.

        Useful for auto-building a {filename: track_index} mapping so the
        masking/auto-EQ tools don't need a hand-written dict.
        """
        clips = []
        for track_index, track in enumerate(self._song.tracks):
            for clip_index, slot in enumerate(track.clip_slots):
                if slot.has_clip and slot.clip.is_audio_clip:
                    clip = slot.clip
                    file_path = None
                    try:
                        file_path = clip.file_path
                    except Exception:
                        file_path = None
                    clips.append({
                        "track_index": track_index,
                        "track_name": track.name,
                        "clip_index": clip_index,
                        "name": clip.name,
                        "file_path": file_path,
                    })
        return {"count": len(clips), "clips": clips}

    def _create_audio_track(self, index):
        """Create a new audio track at the given index (-1 = end)."""
        self._song.create_audio_track(index)
        new_index = len(self._song.tracks) - 1 if index == -1 else index
        t = self._song.tracks[new_index]
        return {"index": new_index, "name": t.name}

    def _delete_arrangement_clips(self, track_index, start=None, end=None):
        """Löscht Arrangement-Clips einer Spur (alle oder im Bereich start..end Beats)."""
        track = self._song.tracks[track_index]
        clips = [c for c in track.arrangement_clips
                 if (start is None or c.start_time >= float(start)) and (end is None or c.start_time < float(end))]
        n = 0
        for c in clips:
            track.delete_clip(c); n += 1
        return {"ok": True, "deleted": n, "remaining": len(track.arrangement_clips)}

    def _duplicate_clip_to_slot(self, track_index, clip_index, target_index):
        """Session-Clip in anderen Slot derselben Spur kopieren (ClipSlot.duplicate_clip_to)."""
        track = self._song.tracks[track_index]
        src = track.clip_slots[clip_index]
        if not src.has_clip:
            raise Exception("No clip in source slot")
        dst = track.clip_slots[target_index]
        if dst.has_clip:
            dst.delete_clip()
        src.duplicate_clip_to(dst)
        return {"ok": True, "name": dst.clip.name}

    # ---------- Automation (Clip-Hüllkurven, Session + Arrangement) ----------
    def _resolve_clip(self, track, params):
        ai = params.get("arrangement_index", None)
        if ai is not None:
            clips = list(track.arrangement_clips)
            if ai < 0 or ai >= len(clips):
                raise IndexError("arrangement_index out of range (%d clips)" % len(clips))
            return clips[ai]
        slot = track.clip_slots[params.get("clip_index", 0)]
        if not slot.has_clip:
            raise Exception("No clip in slot")
        return slot.clip

    def _resolve_param(self, track, params):
        mixer = params.get("mixer", None)
        if mixer == "volume":
            return track.mixer_device.volume
        if mixer == "panning":
            return track.mixer_device.panning
        if mixer is not None and mixer.startswith("send"):
            digits = "".join(ch for ch in mixer[4:] if ch.isdigit())
            idx = int(digits or 0)
            return track.mixer_device.sends[idx]
        dev = track.devices[params.get("device_index", 0)]
        name = params.get("param_name", None)
        pidx = params.get("param_index", None)
        if pidx is not None:
            return dev.parameters[pidx]
        for p in dev.parameters:
            if p.name == name:
                return p
        raise Exception("Parameter '%s' not found on '%s'" % (name, dev.name))

    def _add_clip_automation(self, params):
        """points: [[time_beats, value, (length)]...] value in Parameter-Einheiten (min..max).
        mode 'step' (default) setzt Stufen; mode 'ramp' interpoliert linear zwischen Punkten in 'resolution' Beats."""
        track = self._song.tracks[params.get("track_index", 0)]
        clip = self._resolve_clip(track, params)
        param = self._resolve_param(track, params)
        env = clip.automation_envelope(param)
        if env is None and hasattr(clip, "create_automation_envelope"):
            env = clip.create_automation_envelope(param)
        if env is None:
            raise Exception("Parameter not automatable in this clip")
        pts = sorted([list(p) for p in params.get("points", [])], key=lambda x: x[0])
        mode = params.get("mode", "step"); res = float(params.get("resolution", 0.25))
        lo, hi = param.min, param.max
        clamp = lambda v: max(lo, min(hi, float(v)))
        n = 0
        if mode == "ramp" and len(pts) >= 2:
            for i in range(len(pts) - 1):
                t0, v0 = float(pts[i][0]), clamp(pts[i][1]); t1, v1 = float(pts[i+1][0]), clamp(pts[i+1][1])
                t = t0
                while t < t1 - 1e-6:
                    seg = min(res, t1 - t)
                    v = v0 + (v1 - v0) * ((t - t0) / (t1 - t0)) if t1 > t0 else v1
                    env.insert_step(t, seg, v); n += 1; t += seg
            env.insert_step(float(pts[-1][0]), max(0.01, float(clip.length) - float(pts[-1][0])), clamp(pts[-1][1])); n += 1
        else:
            for i, pt in enumerate(pts):
                t = float(pt[0])
                length = float(pt[2]) if len(pt) > 2 else (float(pts[i+1][0]) - t if i + 1 < len(pts) else max(0.01, float(clip.length) - t))
                env.insert_step(t, length, clamp(pt[1])); n += 1
        return {"ok": True, "param": param.name, "steps": n, "range": [lo, hi], "clip": clip.name}

    def _clear_clip_automation(self, params):
        track = self._song.tracks[params.get("track_index", 0)]
        clip = self._resolve_clip(track, params)
        if params.get("param_name") is None and params.get("param_index") is None and params.get("mixer") is None:
            clip.clear_all_envelopes(); return {"ok": True, "cleared": "all"}
        param = self._resolve_param(track, params)
        clip.clear_envelope(param); return {"ok": True, "cleared": param.name}

    def _set_arrangement_clip(self, params):
        """Arrangement-Clip anpassen: name, loop/end (length in beats), muted."""
        track = self._song.tracks[params.get("track_index", 0)]
        clip = self._resolve_clip(track, params)
        if params.get("name") is not None: clip.name = params["name"]
        if params.get("looping") is not None: clip.looping = bool(params["looping"])
        if params.get("loop_end") is not None: clip.loop_end = float(params["loop_end"])
        if params.get("end_marker") is not None: clip.end_marker = float(params["end_marker"])
        if params.get("muted") is not None: clip.muted = bool(params["muted"])
        return {"ok": True, "name": clip.name, "start": clip.start_time, "end": clip.end_time, "length": clip.length}

    def _arrangement_record(self, params):
        """Eigener Renderer: Master per Resampling als ARRANGEMENT-Aufnahme (durchgehend, taktgenau).
        action=start: Capture-Spur scharf, Loop aus, Arrangement-Record an, Wiedergabe ab start_beat.
          (Live startet ggf. an der Nutzer-Markierung -> Caller prüft position und springt per 'seek'.)
        action=seek: Abspielkopf während der Aufnahme auf start_beat setzen.
        action=stop: Aufnahme beenden, Ausgangszustand wiederherstellen, alle Clips der Capture-Spur melden."""
        song = self._song
        act = params.get("action", "start")
        cap_index, cap = self._ensure_resample_track()
        if act == "start":
            st = {"loop": bool(song.loop), "record_mode": bool(song.record_mode),
                  "solo": [i for i, t in enumerate(song.tracks) if self._safe_attr(t, "solo", False)]}
            self._arr_rec_state = st
            song.stop_playing()
            song.loop = False
            for t in song.tracks:
                if t is not cap and self._safe_attr(t, "arm", False):
                    try:
                        t.arm = 0
                    except Exception:
                        pass
            cap.arm = 1
            try:
                cap.current_monitoring_state = 2   # Off: kein Rückkopplungs-Echo
            except Exception:
                pass
            song.current_song_time = float(params.get("start_beat", 0.0))
            # punch_first: Aufnahme schon beim Start scharf (für Takt 1 ohne Vorlauf).
            song.record_mode = bool(params.get("punch_first", False))
            song.start_playing()
            # Aufnahme erst per action=punch scharf schalten (Live beendet einen Arrangement-Take,
            # sobald man während der Aufnahme springt -> erst springen, dann punch-in).
            return {"capture_track_index": cap_index, "solo_active": st["solo"],
                    "time": song.current_song_time}
        if act == "punch":
            song.record_mode = True
            return {"time": song.current_song_time, "record_mode": bool(song.record_mode)}
        if act == "seek":
            song.current_song_time = float(params.get("start_beat", 0.0))
            return {"time": song.current_song_time}
        if act == "stop":
            song.record_mode = False
            song.stop_playing()
            try:
                cap.arm = 0
            except Exception:
                pass
            st = getattr(self, "_arr_rec_state", {}) or {}
            if "loop" in st:
                song.loop = st["loop"]
            clips = []
            for c in cap.arrangement_clips:
                d = {"start_time": c.start_time, "end_time": c.end_time, "name": c.name}
                try:
                    d["file_path"] = c.file_path
                except Exception:
                    d["file_path"] = None
                clips.append(d)
            if params.get("cleanup", True):
                # Takes nicht im Arrangement liegen lassen (sonst spielen sie beim nächsten Render mit).
                for c in list(cap.arrangement_clips):
                    try:
                        cap.delete_clip(c)
                    except Exception:
                        pass
            return {"capture_track_index": cap_index, "clips": clips}
        raise ValueError("action must be start|seek|punch|stop")

    def _transport_state(self, params):
        """Arrangement-Folgen/Automation lesen und optional zurücksetzen:
        back_to_arranger=True -> alle Spuren folgen wieder dem Arrangement;
        re_enable_automation=True -> überschriebene Automationen wieder aktiv."""
        song = self._song
        if params.get("back_to_arranger"):
            song.back_to_arranger = False
        if params.get("re_enable_automation"):
            try:
                song.re_enable_automation()
            except Exception:
                pass
        return {"back_to_arranger": bool(song.back_to_arranger),
                "re_enable_automation_enabled": bool(getattr(song, "re_enable_automation_enabled", False))}

    def _set_song_loop(self, params):
        # Reihenfolge wichtig: Live verbietet loop_start + (alte) loop_length hinter dem Songende
        # -> erst Länge (verkürzen), dann Start, dann Länge endgültig.
        if params.get("loop") is not None: self._song.loop = bool(params["loop"])
        if params.get("length") is not None:
            self._song.loop_length = min(float(params["length"]), float(self._song.loop_length))
        if params.get("start") is not None: self._song.loop_start = float(params["start"])
        if params.get("length") is not None: self._song.loop_length = float(params["length"])
        if params.get("song_time") is not None: self._song.current_song_time = float(params["song_time"])
        return {"loop": self._song.loop, "start": self._song.loop_start, "length": self._song.loop_length}

    def _clip_by(self, track_index, clip_index, arrangement_index):
        track = self._song.tracks[track_index]
        if arrangement_index is not None:
            return list(track.arrangement_clips)[arrangement_index]
        slot = track.clip_slots[clip_index]
        if not slot.has_clip:
            raise Exception("No clip in slot")
        return slot.clip

    def _get_warp_markers(self, params):
        c = self._clip_by(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
        return {"warping": c.warping, "markers": [{"beat_time": m.beat_time, "sample_time": m.sample_time} for m in c.warp_markers],
                "has_add": hasattr(c, "add_warp_marker"), "has_move": hasattr(c, "move_warp_marker"), "has_remove": hasattr(c, "remove_warp_marker")}

    def _set_warp_markers(self, params):
        """markers: [{beat_time, sample_time}] (Beats / Sekunden). clear=True entfernt vorhandene Marker (außer dem ersten,
        der wird auf den ersten neuen Marker gezogen)."""
        c = self._clip_by(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
        if not c.is_audio_clip:
            raise Exception("Not an audio clip")
        if not hasattr(c, "add_warp_marker"):
            raise Exception("Live API has no add_warp_marker")
        import Live
        c.warping = True
        markers = sorted(params.get("markers", []), key=lambda m: m["beat_time"])
        log = {"removed": 0, "added": 0, "errors": []}
        if params.get("clear", True):
            existing = sorted([m.beat_time for m in c.warp_markers])
            for bt in existing[1:]:
                try:
                    c.remove_warp_marker(bt); log["removed"] += 1
                except Exception as e:
                    log["errors"].append("remove %s: %s" % (bt, e))
        for m in markers:
            bt, stt = float(m["beat_time"]), float(m["sample_time"])
            added = False
            for maker in (lambda: Live.Clip.WarpMarker(beat_time=bt, sample_time=stt),
                          lambda: Live.Clip.WarpMarker(bt, stt),
                          lambda: {"beat_time": bt, "sample_time": stt}):
                try:
                    c.add_warp_marker(maker()); log["added"] += 1; added = True; break
                except Exception as e:
                    last_err = str(e)
            if not added:
                log["errors"].append("add %s: %s" % (m, last_err[:120]))
                if len(log["errors"]) > 3: break
        log["markers_now"] = len(c.warp_markers)
        return log

    def _clip_props(self, params):
        """Audio-Clip (Session: clip_index / Arrangement: arrangement_index) lesen und optional setzen:
        warping, warp_mode (0 Beats,1 Tones,2 Texture,3 Re-Pitch,4 Complex,6 Complex Pro),
        pitch_coarse (-48..48 Halbtöne), pitch_fine (-50..49 Cent), gain (0..1). Ohne Setter = nur lesen."""
        c = self._clip_by(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
        if not c.is_audio_clip:
            raise Exception("Not an audio clip")
        if params.get("warping") is not None:
            c.warping = bool(params["warping"])
        if params.get("warp_mode") is not None:
            c.warp_mode = int(params["warp_mode"])
        if params.get("pitch_coarse") is not None:
            c.pitch_coarse = max(-48, min(48, int(params["pitch_coarse"])))
        if params.get("pitch_fine") is not None:
            c.pitch_fine = max(-50, min(49, int(round(float(params["pitch_fine"])))))
        if params.get("gain") is not None:
            c.gain = max(0.0, min(1.0, float(params["gain"])))
        out = {"name": c.name, "file_path": getattr(c, "file_path", None), "warping": c.warping,
               "warp_mode": int(c.warp_mode), "pitch_coarse": c.pitch_coarse, "pitch_fine": c.pitch_fine,
               "gain": c.gain, "gain_db": getattr(c, "gain_display_string", None), "length": c.length,
               "start_marker": c.start_marker, "end_marker": c.end_marker, "loop_start": c.loop_start, "loop_end": c.loop_end, "looping": c.looping}
        try:
            out["markers"] = [{"beat_time": m.beat_time, "sample_time": m.sample_time} for m in c.warp_markers]
        except Exception:
            pass
        if params.get("arrangement_index") is not None:
            out["start_time"] = c.start_time
            out["end_time"] = c.end_time
        return out

    # ---------- Racks bauen (Live 12.3+: insert_device / insert_chain / replace_sample) ----------
    def _hot_reload(self):
        """Script-Code neu laden ohne Ableton-Neustart: Modul neu importieren, Klasse der laufenden
        Instanz austauschen (Zustand/Socket bleiben erhalten)."""
        import importlib, sys
        mod = sys.modules[self.__class__.__module__]
        importlib.reload(mod)
        self.__class__ = mod.AbletonMCP
        return {"reloaded": True, "module": mod.__name__}

    def _set_song_scale(self, params):
        """Globale Tonart setzen (Live 12): root_note 0-11 (0=C), scale_name z. B. 'Minor', scale_mode an/aus."""
        song = self._song
        if params.get("root_note") is not None:
            song.root_note = int(params["root_note"])
        if params.get("scale_name") is not None:
            song.scale_name = str(params["scale_name"])
        if params.get("scale_mode") is not None:
            song.scale_mode = bool(params["scale_mode"])
        return self._get_song_scale()

    def _set_clip_notes(self, params):
        """Alle Noten eines MIDI-Clips ersetzen (Session: clip_index, Arrangement: arrangement_index).
        notes: [{pitch, start_time, duration, velocity, mute}]. Gibt die alten Noten zurück (für Undo)."""
        import Live
        c = self._clip_by(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
        if not c.is_midi_clip:
            raise Exception("Not a MIDI clip")
        old = [{"pitch": x.pitch, "start_time": x.start_time, "duration": x.duration, "velocity": x.velocity, "mute": x.mute}
               for x in c.get_notes_extended(0, 128, -1000.0, 100000.0)]
        c.remove_notes_extended(0, 128, -1000.0, 100000.0)
        specs = [Live.Clip.MidiNoteSpecification(pitch=int(n["pitch"]), start_time=float(n["start_time"]),
                 duration=float(n["duration"]), velocity=float(n.get("velocity", 100)), mute=bool(n.get("mute", False)))
                 for n in params.get("notes", [])]
        if specs:
            c.add_new_notes(tuple(specs))
        return {"ok": True, "old": old, "count": len(specs), "clip": c.name}

    def _create_arrangement_audio_clip(self, params):
        """Audiodatei als Arrangement-Clip auf eine Audiospur legen (Track.create_audio_clip, Live 12)."""
        track = self._song.tracks[int(params.get("track_index", 0))]
        clip = track.create_audio_clip(str(params["path"]), float(params.get("position", 0.0)))
        return {"ok": True, "name": clip.name, "start": clip.start_time, "end": clip.end_time}

    def _create_return_track(self, params):
        """Neue Return-Spur am Ende anlegen (Song.create_return_track), optional mit Name."""
        self._song.create_return_track()
        rt = self._song.return_tracks[len(self._song.return_tracks) - 1]
        if params.get("name"):
            rt.name = str(params["name"])
        return {"return_index": len(self._song.return_tracks) - 1, "name": rt.name,
                "returns": [t.name for t in self._song.return_tracks]}

    def _rack_insert_chain(self, params):
        """Leere Kette ans Ende eines Racks (Audio/Instrument) hängen, optional mit Name."""
        track, rack = self._rack_of(params)
        rack.insert_chain()
        ch = rack.chains[len(rack.chains) - 1]
        if params.get("name"):
            ch.name = str(params["name"])
        return {"chains": [c.name for c in rack.chains], "chain_index": len(rack.chains) - 1}

    def _create_arrangement_midi_clip(self, params):
        """Leeren MIDI-Clip im Arrangement anlegen (Track.create_midi_clip, Live 12): start + length in Beats.
        Optional name und notes (wie set_clip_notes, Zeiten relativ zum Clip-Anfang)."""
        import Live
        track = self._song.tracks[int(params.get("track_index", 0))]
        start = float(params["start"]); length = float(params["length"])
        clip = track.create_midi_clip(start, length)
        if params.get("name"):
            clip.name = str(params["name"])
        specs = [Live.Clip.MidiNoteSpecification(pitch=int(n["pitch"]), start_time=float(n["start_time"]),
                 duration=float(n["duration"]), velocity=float(n.get("velocity", 100)), mute=bool(n.get("mute", False)))
                 for n in params.get("notes", [])]
        if specs:
            clip.add_new_notes(tuple(specs))
        idx = [i for i, c in enumerate(track.arrangement_clips) if abs(c.start_time - start) < 1e-6]
        return {"ok": True, "name": clip.name, "start": clip.start_time, "end": clip.end_time,
                "arrangement_index": idx[0] if idx else None, "notes": len(specs)}

    def _set_fold(self, params):
        """Gruppenspur auf-/zuklappen (fold: 0 = auf, 1 = zu). Ohne track_index: alle Gruppen."""
        out = []
        idx = params.get("track_index")
        for i, t in enumerate(self._song.tracks):
            if getattr(t, "is_foldable", False) and (idx is None or int(idx) == i):
                t.fold_state = int(params.get("fold", 0))
                out.append(t.name)
        return {"changed": out}

    def _rack_of(self, params):
        track = self._tracks_of(params.get("track_type", "track"))[int(params.get("track_index", 0))]
        dev = track.devices[int(params.get("device_index", 0))]
        if not hasattr(dev, "chains"):
            raise Exception("Device '%s' is not a rack" % dev.name)
        return track, dev

    def _insert_device(self, params):
        """Natives Live-Gerät per UI-Namen einfügen (Spur oder Kette eines Racks)."""
        track = self._tracks_of(params.get("track_type", "track"))[int(params.get("track_index", 0))]
        target = track
        if params.get("chain_index") is not None:
            target = track.devices[int(params.get("device_index", 0))].chains[int(params["chain_index"])]
        idx = params.get("target_index")
        if idx is None:
            target.insert_device(str(params["device_name"]))
        else:
            target.insert_device(str(params["device_name"]), int(idx))
        return {"devices": [d.name for d in target.devices]}

    def _set_param_display(self, dev, name, want):
        """Parameter per Anzeige-Text setzen (Enums) oder per Zahl (Rohwert)."""
        for p in dev.parameters:
            if p.name == name:
                if isinstance(want, str):
                    lo, hi = int(p.min), int(p.max)
                    for v in range(lo, hi + 1):
                        if p.str_for_value(v).strip().lower() == want.lower():
                            p.value = v
                            return True
                    return False
                p.value = max(p.min, min(p.max, float(want)))
                return True
        return False

    def _set_param_db(self, dev, name, db):
        """Parameter so setzen, dass die Anzeige ~db zeigt (Binärsuche über str_for_value)."""
        import re
        def num(txt):
            m = re.match(r"\s*([+-]?[0-9.]+|-inf)", txt.replace(",", "."))
            if not m: return None
            return -1e9 if m.group(1) == "-inf" else float(m.group(1))
        for p in dev.parameters:
            if p.name == name:
                lo, hi = float(p.min), float(p.max)
                for _ in range(40):
                    mid = (lo + hi) / 2
                    v = num(p.str_for_value(mid))
                    if v is None: break
                    if abs(v - db) < 0.05: break
                    if v < db: lo = mid
                    else: hi = mid
                p.value = mid
                return p.str_for_value(mid)
        return None

    def _rack_add_sample_chain(self, params):
        """Neue Kette mit Simpler + Sample ins Rack: name, path, transpose (st), detune (ct),
        volume (Simpler-Rohwert), trigger (bool), mute (bool)."""
        track, rack = self._rack_of(params)
        rack.insert_chain()
        ch = rack.chains[len(rack.chains) - 1]
        if params.get("name"):
            ch.name = str(params["name"])
        ch.insert_device("Simpler")
        sim = ch.devices[0]
        sim.replace_sample(str(params["path"]))
        ok = {}
        if params.get("trigger", True):
            ok["trigger"] = self._set_param_display(sim, "Trigger Mode", "Trigger")
        if params.get("transpose") is not None:
            ok["transpose"] = self._set_param_display(sim, "Transpose", float(params["transpose"]))
        if params.get("detune") is not None:
            ok["detune"] = self._set_param_display(sim, "Detune", float(params["detune"]))
        if params.get("volume") is not None:
            ok["volume"] = self._set_param_display(sim, "Volume", float(params["volume"]))
        if params.get("volume_db") is not None:
            ok["volume_db"] = self._set_param_db(sim, "Volume", float(params["volume_db"]))
        ch.mute = bool(params.get("mute", False))
        return {"chain_index": len(rack.chains) - 1, "name": ch.name, "device": sim.name, "set": ok}

    def _rack_info(self, params):
        track, rack = self._rack_of(params)
        return {"rack": rack.name, "chains": [{"i": i, "name": c.name, "mute": c.mute,
                "devices": [d.name for d in c.devices]} for i, c in enumerate(rack.chains)]}

    def _rack_select_chain(self, params):
        """Nur Kette 'index' hörbar (alle anderen stumm) – Fallback ohne Chain-Selector-Zonen."""
        track, rack = self._rack_of(params)
        k = int(params["index"])
        if k < 0:  # -1 = alle Ketten hörbar (Auswahl übernimmt der Chain Selector)
            for c in rack.chains:
                c.mute = False
            return {"selected": "all"}
        for i, c in enumerate(rack.chains):
            c.mute = (i != k)
        return {"selected": k, "name": rack.chains[k].name}

    def _get_cue_points(self):
        """Alle Arrangement-Locators, zeitlich sortiert (time in Beats)."""
        cues = sorted(self._song.cue_points, key=lambda q: q.time)
        return {"cue_points": [{"name": q.name, "time": q.time} for q in cues]}

    def _cue_point(self, params):
        """Locator an 'time' (Beats) anlegen/umbenennen ('name') oder löschen ('delete': true).
        Die LOM kann Locators nur an der Abspielposition setzen -> current_song_time wird kurz versetzt."""
        t = float(params["time"])
        def at(tt):
            return [q for q in self._song.cue_points if abs(q.time - tt) < 1e-3]
        hit = at(t)
        if params.get("delete"):
            if hit:
                self._song.current_song_time = t
                self._song.set_or_delete_cue()
            return {"deleted": bool(hit), "time": t}
        if not hit:
            before = set(id(q) for q in self._song.cue_points)
            self._song.current_song_time = t
            self._song.set_or_delete_cue()
            hit = at(t) or [q for q in self._song.cue_points if id(q) not in before]
        if not hit:
            raise Exception("Locator konnte nicht angelegt werden")
        if params.get("name") is not None:
            hit[0].name = str(params["name"])
        return {"name": hit[0].name, "time": hit[0].time, "requested_time": t}

    def _get_song_scale(self):
        """Globale Tonart/Skala (Live 12+). In Live 11 fehlen die Attribute -> available False."""
        s = self._song
        if not hasattr(s, "root_note"):
            return {"available": False}
        return {"available": True, "root_note": int(s.root_note), "scale_name": str(getattr(s, "scale_name", "")),
                "scale_mode": bool(getattr(s, "scale_mode", False)),
                "scale_intervals": [int(i) for i in getattr(s, "scale_intervals", [])],
                "tempo": s.tempo}

    def _remove_clip_notes(self, params):
        c = self._clip_by(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
        if not c.is_midi_clip:
            raise Exception("Not a MIDI clip")
        c.remove_notes_extended(int(params.get("from_pitch", 0)), int(params.get("pitch_span", 128)),
                                float(params.get("from_time", 0.0)), float(params.get("time_span", c.length)))
        return {"ok": True}

    def _transpose_clip(self, params):
        """Noten eines MIDI-Clips um 'semitones' verschieben; optional 'scale' (Liste Pitch-Classes 0-11)
        -> jede Note danach auf den nächsten Skalenton gezogen. 'pitch_min'/'pitch_max' begrenzen den Bereich."""
        import Live
        c = self._clip_by(params.get("track_index", 0), params.get("clip_index", 0), params.get("arrangement_index", None))
        if not c.is_midi_clip:
            raise Exception("Not a MIDI clip")
        semi = int(params.get("semitones", 0)); scale = params.get("scale", None)
        pmin = int(params.get("pitch_min", 0)); pmax = int(params.get("pitch_max", 127))
        notes = c.get_notes_extended(0, 128, 0.0, c.length)
        specs = []; changed = 0
        for x in notes:
            p = x.pitch
            if pmin <= p <= pmax:
                p = p + semi
                if scale:
                    best = min(range(p - 6, p + 7), key=lambda q: (abs(q - p), q < p) if (q % 12) in scale else (99, 0))
                    p = best
                p = max(0, min(127, p))
                if p != x.pitch: changed += 1
            specs.append(Live.Clip.MidiNoteSpecification(pitch=p, start_time=x.start_time, duration=x.duration,
                                                         velocity=x.velocity, mute=x.mute))
        c.remove_notes_extended(0, 128, 0.0, c.length)
        c.add_new_notes(tuple(specs))
        return {"ok": True, "notes": len(specs), "changed": changed, "clip": c.name}

    def _get_clip_notes(self, track_index, clip_index, arrangement_index=None):
        """Read all notes of a MIDI clip (Live 11+ get_notes_extended)."""
        c = self._clip_by(track_index, clip_index, arrangement_index)
        if not c.is_midi_clip:
            raise Exception("No MIDI clip")
        notes = c.get_notes_extended(0, 128, 0.0, c.length)
        return {"name": c.name, "length": c.length,
                "notes": [{"pitch": x.pitch, "start_time": x.start_time, "duration": x.duration,
                           "velocity": x.velocity, "mute": x.mute} for x in notes]}

    def _delete_track(self, track_index):
        """Delete a track (Song.delete_track). Returns the deleted name and new count."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        name = self._song.tracks[track_index].name
        self._song.delete_track(track_index)
        return {"ok": True, "deleted": name, "track_count": len(self._song.tracks)}

    def _delete_clip(self, track_index, clip_index):
        """Delete the clip in a session slot (ClipSlot.delete_clip)."""
        track = self._song.tracks[track_index]
        slot = track.clip_slots[clip_index]
        if not slot.has_clip:
            raise Exception("No clip in slot")
        name = slot.clip.name
        slot.delete_clip()
        return {"ok": True, "deleted": name}

    def _safe_attr(self, obj, name, default=None):
        try:
            return getattr(obj, name)
        except Exception:
            return default

    def _safe_group_name(self, track):
        g = self._safe_attr(track, "group_track", None)
        return g.name if g is not None else None

    def _param_str(self, param):
        try:
            return param.str_for_value(param.value)
        except Exception:
            return None

    def _set_mixer(self, params):
        """Mixer einer Spur setzen. track_type: track | return | master.
        volume/panning/sends als Live-Rohwerte (Volume 0.85 = 0 dB, Pan -1..1,
        Sends 0..1). sends: {"0": 0.2, "1": 0.0} oder Liste (None = unverändert).
        mute/solo nur für normale und Return-Spuren."""
        tracks = self._tracks_of(params.get("track_type", "track"))
        idx = int(params.get("track_index", 0))
        if idx < 0 or idx >= len(tracks):
            raise IndexError("track_index %d out of range (valid 0..%d)" % (idx, len(tracks) - 1))
        track = tracks[idx]
        md = track.mixer_device
        if params.get("volume") is not None:
            md.volume.value = max(md.volume.min, min(md.volume.max, float(params["volume"])))
        if params.get("panning") is not None:
            md.panning.value = max(md.panning.min, min(md.panning.max, float(params["panning"])))
        sends = params.get("sends")
        if sends is not None:
            items = sends.items() if isinstance(sends, dict) else enumerate(sends)
            for k, v in items:
                if v is None:
                    continue
                sd = md.sends[int(k)]
                sd.value = max(sd.min, min(sd.max, float(v)))
        if params.get("mute") is not None:
            track.mute = bool(params["mute"])
        if params.get("solo") is not None:
            track.solo = bool(params["solo"])
        return {
            "ok": True, "name": track.name,
            "volume": md.volume.value, "volume_db": self._param_str(md.volume),
            "panning": md.panning.value,
            "sends": [sd.value for sd in md.sends] if hasattr(md, "sends") else [],
            "sends_db": [self._param_str(sd) for sd in md.sends] if hasattr(md, "sends") else [],
            "mute": self._safe_attr(track, "mute", None), "solo": self._safe_attr(track, "solo", None),
        }

    # ── Ausbau 2026-10-14: Meter, Racks, Routing, Clips, Samples ──────────────
    def _all_tracks_typed(self):
        out = [("track", i, t) for i, t in enumerate(self._song.tracks)]
        out += [("return", i, t) for i, t in enumerate(self._song.return_tracks)]
        out.append(("master", 0, self._song.master_track))
        return out

    def _get_meters(self):
        """Momentaufnahme der Ausgangsmeter aller Spuren (0..1 linear, Live-Skala)."""
        import math
        rows = []
        for ttype, i, t in self._all_tracks_typed():
            l = self._safe_attr(t, "output_meter_left", 0.0) or 0.0
            r = self._safe_attr(t, "output_meter_right", 0.0) or 0.0
            pk = max(l, r)
            rows.append({"type": ttype, "index": i, "name": t.name, "left": l, "right": r,
                         "db": (20.0 * math.log10(pk) if pk > 0 else None)})
        return {"playing": bool(self._song.is_playing), "time": self._song.current_song_time, "meters": rows}

    def _track_from(self, params):
        tracks = self._tracks_of(params.get("track_type", "track"))
        idx = int(params.get("track_index", 0))
        if idx < 0 or idx >= len(tracks):
            raise IndexError("track_index %d out of range (valid 0..%d)" % (idx, len(tracks) - 1))
        return tracks[idx]

    def _device_by_path(self, track, path):
        """path: [device_index, chain_index, device_index, chain_index, ...] — beliebig tief in Racks."""
        path = [int(x) for x in (path or [])]
        if not path:
            raise ValueError("device_path required")
        dev = list(track.devices)[path[0]]
        rest = path[1:]
        while rest:
            if len(rest) < 2:
                raise ValueError("device_path needs pairs of chain_index, device_index after the first device")
            chains = list(self._safe_attr(dev, "chains", []) or [])
            if not chains:
                raise ValueError("Device '%s' is not a rack" % dev.name)
            chain = chains[rest[0]]
            dev = list(chain.devices)[rest[1]]
            rest = rest[2:]
        return dev

    def _device_summary(self, dev, with_params=False):
        d = {"name": dev.name, "class_name": dev.class_name,
             "is_active": bool(self._safe_attr(dev, "is_active", True)),
             "can_have_chains": bool(self._safe_attr(dev, "can_have_chains", False))}
        if with_params:
            d["parameters"] = [{"index": k, "name": p.name, "value": p.value,
                                "display_value": self._param_str(p)} for k, p in enumerate(dev.parameters)]
        return d

    def _rack_tree(self, dev, depth=0, max_depth=6):
        node = self._device_summary(dev)
        chains = list(self._safe_attr(dev, "chains", []) or [])
        if chains and depth < max_depth:
            node["chains"] = []
            for ci, ch in enumerate(chains):
                md = self._safe_attr(ch, "mixer_device", None)
                node["chains"].append({
                    "index": ci, "name": ch.name,
                    "mute": bool(self._safe_attr(ch, "mute", False)),
                    "solo": bool(self._safe_attr(ch, "solo", False)),
                    "volume_db": self._param_str(md.volume) if md is not None else None,
                    "devices": [dict(self._rack_tree(d2, depth + 1, max_depth), index=di)
                                for di, d2 in enumerate(ch.devices)],
                })
        return node

    def _get_rack(self, params):
        track = self._track_from(params)
        dev = self._device_by_path(track, params.get("device_path") or [params.get("device_index", 0)])
        return self._rack_tree(dev)

    def _get_nested_device_parameters(self, params):
        track = self._track_from(params)
        dev = self._device_by_path(track, params.get("device_path"))
        return self._device_summary(dev, with_params=True)

    def _set_nested_device_parameter(self, params):
        track = self._track_from(params)
        dev = self._device_by_path(track, params.get("device_path"))
        plist = list(dev.parameters)
        if params.get("param_index") is not None:
            p = plist[int(params["param_index"])]
        else:
            want = str(params.get("param_name", "")).lower()
            cand = [p for p in plist if p.name.lower() == want]
            if not cand:
                raise ValueError("Parameter '%s' not found on '%s'" % (params.get("param_name"), dev.name))
            p = cand[0]
        v = float(params.get("value"))
        p.value = max(p.min, min(p.max, v))
        return {"device": dev.name, "parameter_name": p.name, "value": p.value, "display_value": self._param_str(p)}

    def _set_rack_chain(self, params):
        track = self._track_from(params)
        dev = self._device_by_path(track, params.get("device_path"))
        ch = list(dev.chains)[int(params.get("chain_index", 0))]
        if params.get("mute") is not None: ch.mute = bool(params["mute"])
        if params.get("solo") is not None: ch.solo = bool(params["solo"])
        if params.get("volume") is not None: ch.mixer_device.volume.value = float(params["volume"])
        return {"chain": ch.name, "mute": bool(ch.mute), "solo": bool(ch.solo),
                "volume_db": self._param_str(ch.mixer_device.volume)}

    def _io_info(self, io):
        def nm(x):
            return self._safe_attr(x, "display_name", None) if x is not None else None
        return {"routing_type": nm(self._safe_attr(io, "routing_type", None)),
                "routing_channel": nm(self._safe_attr(io, "routing_channel", None)),
                "available_types": [nm(x) for x in (self._safe_attr(io, "available_routing_types", []) or [])],
                "available_channels": [nm(x) for x in (self._safe_attr(io, "available_routing_channels", []) or [])]}

    def _get_device_io(self, params):
        """Sidechain-/Geräte-I/O (Live 11+: Device.input_routings / output_routings, falls vorhanden)."""
        track = self._track_from(params)
        dev = self._device_by_path(track, params.get("device_path") or [params.get("device_index", 0)])
        ins = list(self._safe_attr(dev, "input_routings", []) or [])
        outs = list(self._safe_attr(dev, "output_routings", []) or [])
        return {"device": dev.name, "supported": bool(ins or outs),
                "inputs": [self._io_info(x) for x in ins], "outputs": [self._io_info(x) for x in outs]}

    def _pick_routing(self, available, want):
        want = str(want).lower()
        for x in available:
            if (self._safe_attr(x, "display_name", "") or "").lower() == want:
                return x
        for x in available:
            if want in (self._safe_attr(x, "display_name", "") or "").lower():
                return x
        raise ValueError("Routing '%s' not available (%s)" % (
            want, ", ".join(self._safe_attr(x, "display_name", "?") for x in available)))

    def _set_device_io(self, params):
        track = self._track_from(params)
        dev = self._device_by_path(track, params.get("device_path") or [params.get("device_index", 0)])
        ins = list(self._safe_attr(dev, "input_routings", []) or [])
        if not ins:
            raise Exception("Device '%s' exposes no input routings via the Live API" % dev.name)
        io = ins[int(params.get("io_index", 0))]
        if params.get("routing_type"):
            io.routing_type = self._pick_routing(io.available_routing_types, params["routing_type"])
        if params.get("routing_channel"):
            io.routing_channel = self._pick_routing(io.available_routing_channels, params["routing_channel"])
        return self._io_info(io)

    def _get_routing(self, params):
        t = self._track_from(params)
        def nm(x):
            return self._safe_attr(x, "display_name", None) if x is not None else None
        mon = self._safe_attr(t, "current_monitoring_state", None)
        return {"name": t.name,
                "input_type": nm(self._safe_attr(t, "input_routing_type", None)),
                "input_channel": nm(self._safe_attr(t, "input_routing_channel", None)),
                "output_type": nm(self._safe_attr(t, "output_routing_type", None)),
                "output_channel": nm(self._safe_attr(t, "output_routing_channel", None)),
                "monitoring": {0: "In", 1: "Auto", 2: "Off"}.get(mon, mon),
                "available_inputs": [nm(x) for x in (self._safe_attr(t, "available_input_routing_types", []) or [])],
                "available_outputs": [nm(x) for x in (self._safe_attr(t, "available_output_routing_types", []) or [])]}

    def _set_routing(self, params):
        t = self._track_from(params)
        if params.get("input_type"):
            t.input_routing_type = self._pick_routing(t.available_input_routing_types, params["input_type"])
        if params.get("input_channel"):
            t.input_routing_channel = self._pick_routing(t.available_input_routing_channels, params["input_channel"])
        if params.get("output_type"):
            t.output_routing_type = self._pick_routing(t.available_output_routing_types, params["output_type"])
        if params.get("output_channel"):
            t.output_routing_channel = self._pick_routing(t.available_output_routing_channels, params["output_channel"])
        if params.get("monitoring") is not None:
            m = params["monitoring"]
            t.current_monitoring_state = {"in": 0, "auto": 1, "off": 2}.get(str(m).lower(), m)
        return self._get_routing(params)

    def _copy_arrangement_clip(self, params):
        """Arrangement-Clip an eine andere Beat-Position kopieren (move=True löscht das Original)."""
        track = self._song.tracks[int(params.get("track_index", 0))]
        clips = list(track.arrangement_clips)
        ai = int(params.get("arrangement_index", 0))
        clip = clips[ai]
        dest = float(params.get("destination_time"))
        track.duplicate_clip_to_arrangement(clip, dest)
        if params.get("move"):
            track.delete_clip(clip)
        return {"ok": True, "name": clip.name if not params.get("move") else None,
                "destination_time": dest, "moved": bool(params.get("move")),
                "clip_count": len(track.arrangement_clips)}

    def _load_sample_file(self, params):
        """Sample per Dateiname aus dem Browser (Aktuelles Projekt, User-Ordner, Samples,
        User Library) laden. Auf eine MIDI-Spur geladen entsteht ein Simpler."""
        track = self._track_from(params)
        want = str(params.get("file_name", "")).lower()
        if not want:
            raise ValueError("file_name required")
        app = self.application()
        b = app.browser
        roots = []
        for attr in ("current_project", "samples", "user_library"):
            r = self._safe_attr(b, attr, None)
            if r is not None:
                roots.append(r)
        for uf in (self._safe_attr(b, "user_folders", []) or []):
            roots.append(uf)
        found = None
        stack = [(r, 0) for r in roots]
        while stack and found is None:
            item, depth = stack.pop()
            try:
                if (item.name or "").lower() == want and getattr(item, "is_loadable", False):
                    found = item
                    break
                if depth < 8 and getattr(item, "is_folder", False) or depth == 0:
                    for ch in item.children:
                        stack.append((ch, depth + 1))
            except Exception:
                continue
        if found is None:
            raise ValueError("Sample '%s' not found in browser (Current Project, Samples, User Library, User Folders)" % params.get("file_name"))
        self._song.view.selected_track = track
        b.load_item(found)
        return {"loaded": True, "item": found.name, "track": track.name,
                "devices": [d.name for d in track.devices]}

    def _set_track_volume(self, track_index, volume):
        """Set mixer volume (0.0-1.0, 0.85 = 0 dB)."""
        track = self._song.tracks[track_index]
        track.mixer_device.volume.value = float(volume)
        return {"ok": True, "volume": track.mixer_device.volume.value}

    def _get_clip_info(self, track_index, clip_index):
        """Read clip state incl. warp settings for audio clips."""
        slot = self._song.tracks[track_index].clip_slots[clip_index]
        if not slot.has_clip:
            raise Exception("No clip in slot")
        c = slot.clip
        info = {"name": c.name, "length": c.length, "is_audio_clip": c.is_audio_clip,
                "is_midi_clip": c.is_midi_clip, "looping": c.looping,
                "loop_start": c.loop_start, "loop_end": c.loop_end,
                "start_marker": c.start_marker, "end_marker": c.end_marker}
        if c.is_audio_clip:
            info.update({"warping": c.warping, "warp_mode": int(c.warp_mode),
                         "file_path": getattr(c, "file_path", None),
                         "sample_length": getattr(c, "sample_length", None)})
            try:
                info["warp_markers"] = [{"beat_time": m.beat_time, "sample_time": m.sample_time}
                                        for m in c.warp_markers][:8]
            except Exception:
                pass
        return info

    def _set_clip_warp(self, track_index, clip_index, warping=None, warp_mode=None,
                       loop_start=None, loop_end=None, start_marker=None, looping=None):
        """Set warp/loop properties of an audio clip.
        warp_mode: 0 Beats, 1 Tones, 2 Texture, 3 Re-Pitch, 4 Complex, 6 Complex Pro (Live enum).
        """
        slot = self._song.tracks[track_index].clip_slots[clip_index]
        if not slot.has_clip:
            raise Exception("No clip in slot")
        c = slot.clip
        if warping is not None and c.is_audio_clip:
            c.warping = bool(warping)
        if warp_mode is not None and c.is_audio_clip:
            c.warp_mode = int(warp_mode)
        if looping is not None:
            c.looping = bool(looping)
        if loop_end is not None:
            c.loop_end = float(loop_end)
        if loop_start is not None:
            c.loop_start = float(loop_start)
        if start_marker is not None:
            c.start_marker = float(start_marker)
        return self._get_clip_info(track_index, clip_index)

    def _delete_device(self, track_index, device_index):
        """Delete a device from a track's chain.

        The Live API has delete_device but NO move/reorder — reordering would
        mean delete + reload (losing all settings), so this only deletes.
        """
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        name = track.devices[device_index].name
        track.delete_device(device_index)
        return {"ok": True, "deleted": name, "remaining_devices": len(track.devices)}

    def _set_resampling_input(self, track):
        """Set a track's input routing to 'Resampling' (captures master out)."""
        try:
            for rt in track.available_input_routing_types:
                if "resampl" in rt.display_name.lower():
                    track.input_routing_type = rt
                    return True
        except Exception as e:
            self.log_message("Could not set Resampling input: " + str(e))
        return False

    def _ensure_resample_track(self, name="MCP Capture"):
        """Find or create a dedicated audio track set to Resampling input."""
        for i, t in enumerate(self._song.tracks):
            if t.name == name:
                self._set_resampling_input(t)
                return i, t
        self._song.create_audio_track(-1)
        i = len(self._song.tracks) - 1
        t = self._song.tracks[i]
        try:
            t.name = name
        except Exception:
            pass
        self._set_resampling_input(t)
        return i, t

    def _start_resample_capture(self, source_track_index=None, start_time=None, mute_tracks=None):
        """Start a real-time resampling recording.

        The LOM has no offline bounce, so this records the master output in
        real time. If source_track_index is given, that track is soloed so the
        master contains only it. Caller plays for the desired length, then
        calls stop_resample_capture.
        """
        song = self._song
        cap_index, cap = self._ensure_resample_track()

        soloed = None
        if (source_track_index is not None and 0 <= source_track_index < len(song.tracks)
                and source_track_index != cap_index):
            try:
                song.tracks[source_track_index].solo = 1
                soloed = source_track_index
            except Exception:
                pass

        # Optional: Spuren stummschalten statt solo (Messen "im Mix" — Solo verfälscht
        # Sidechain-Effekte, deren Quelle beim Solo verstummt). Vorzustand merken.
        self._capture_muted = []
        for mi in (mute_tracks or []):
            try:
                t = song.tracks[int(mi)]
                self._capture_muted.append((int(mi), bool(t.mute)))
                t.mute = 1
            except Exception:
                pass

        cap.arm = 1
        slot = cap.clip_slots[0]
        if slot.has_clip:
            try:
                slot.delete_clip()
            except Exception:
                pass
        # Startposition: ausdrücklich gesetzt > Loop-Anfang (wenn Loop an) > 0.
        if start_time is not None:
            song.current_song_time = float(start_time)
        elif song.loop:
            song.current_song_time = float(song.loop_start)
        else:
            song.current_song_time = 0.0
        slot.fire()            # armed audio slot + fire = record
        song.start_playing()
        # Live startet je nach Zustand am Insert-Marker statt an current_song_time (z. B. nach
        # Absturz-Wiederherstellung, Loop aus) -> Position nach dem Start nochmals setzen.
        if start_time is not None:
            song.current_song_time = float(start_time)
        return {"capture_track_index": cap_index, "soloed_source": soloed, "recording": True}

    def _stop_resample_capture(self, capture_track_index, source_track_index=None):
        """Stop a resampling recording and return the captured clip's path."""
        song = self._song
        if capture_track_index < 0 or capture_track_index >= len(song.tracks):
            raise IndexError("Capture track index out of range")
        cap = song.tracks[capture_track_index]
        slot = cap.clip_slots[0]
        try:
            slot.stop()
        except Exception:
            pass
        song.stop_playing()
        try:
            cap.arm = 0
        except Exception:
            pass
        if source_track_index is not None and 0 <= source_track_index < len(song.tracks):
            try:
                song.tracks[source_track_index].solo = 0
            except Exception:
                pass
        for mi, was in getattr(self, "_capture_muted", []) or []:
            try:
                song.tracks[mi].mute = was
            except Exception:
                pass
        self._capture_muted = []

        file_path = None
        has_clip = slot.has_clip
        if has_clip and slot.clip.is_audio_clip:
            try:
                file_path = slot.clip.file_path
            except Exception:
                file_path = None
        return {
            "capture_track_index": capture_track_index,
            "clip_index": 0,
            "has_clip": has_clip,
            "file_path": file_path,
            "note": ("Freshly recorded clips may have an empty file_path until "
                     "the Set is saved; if so, save the Set then call "
                     "get_clip_sample_path."),
        }

    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise
    
    
    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_audio_clip(self, track_index, clip_index, path):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Must be an audio track. Audio tracks expose audio input; MIDI
            # tracks don't. Reject MIDI / return tracks up front so the caller
            # gets a clear error instead of a Live API exception.
            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise
    
    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise
    
    
    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time_val):
        """Move the arrangement playhead to a position in beats"""
        try:
            self._song.current_song_time = float(time_val)
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    "is_playing": clip.is_playing
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index, clip_index, destination_time):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_browser_item(self, track_index, item_uri, track_type="track"):
        """Load a browser item onto a track by its URI (track_type: track | return | master)"""
        try:
            tracks = self._tracks_of(track_type)
            if track_index < 0 or track_index >= len(tracks):
                raise IndexError("Track index out of range")

            track = tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. If no marker
    # matches we fall back to the default order, so this is purely an
    # optimisation — never a correctness change.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        """
        if current_depth == 0:
            cache = getattr(self, '_uri_cache', None)
            if cache is None:
                self._uri_cache = cache = {}
            if uri in cache:
                return cache[uri]
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                cache[uri] = result
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"
    
    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # Helper function to process a browser item and its children
            def process_item(item, depth=0):
                if not item:
                    return None
                
                result = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": hasattr(item, 'children') and bool(item.children),
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }
                
                
                return result
            
            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
