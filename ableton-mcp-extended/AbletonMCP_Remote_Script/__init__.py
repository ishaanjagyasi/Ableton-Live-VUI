# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import socket
import json
import threading
import time
import traceback
import difflib

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
HOST = "localhost"

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
            # Route the command to the appropriate handler
            if command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_track_info(track_index)
            elif command_type == "get_device_parameters":
                track_index = params.get("track_index", 0)
                device_index = params.get("device_index", 0)
                response["result"] = self._get_device_parameters(track_index, device_index)
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in ["create_midi_track", "create_audio_track", "delete_track", "duplicate_track",
                                 "set_track_name", "set_track_volume", "set_track_pan",
                                 "arm_track", "mute_track", "solo_track", "set_track_color",
                                 "create_clip", "add_notes_to_clip", "set_clip_name",
                                 "set_tempo", "fire_clip", "stop_clip",
                                 "start_playback", "stop_playback", "load_browser_item",
                                 "get_all_browser_items", "fuzzy_search_browser", "load_device_by_name",
                                 "set_device_parameter",
                                 "get_track_routing_options", "set_track_output_routing",
                                 "set_track_input_routing", "set_track_monitoring"]:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "create_audio_track":
                            index = params.get("index", -1)
                            result = self._create_audio_track(index)
                        elif command_type == "delete_track":
                            track_index = params.get("track_index", 0)
                            result = self._delete_track(track_index)
                        elif command_type == "duplicate_track":
                            track_index = params.get("track_index", 0)
                            result = self._duplicate_track(track_index)
                        elif command_type == "set_track_volume":
                            track_index = params.get("track_index", 0)
                            volume = params.get("volume", 0.85)
                            result = self._set_track_volume(track_index, volume)
                        elif command_type == "set_track_pan":
                            track_index = params.get("track_index", 0)
                            pan = params.get("pan", 0.0)
                            result = self._set_track_pan(track_index, pan)
                        elif command_type == "arm_track":
                            track_index = params.get("track_index", 0)
                            armed = params.get("armed", True)
                            result = self._arm_track(track_index, armed)
                        elif command_type == "mute_track":
                            track_index = params.get("track_index", 0)
                            muted = params.get("muted", True)
                            result = self._mute_track(track_index, muted)
                        elif command_type == "solo_track":
                            track_index = params.get("track_index", 0)
                            soloed = params.get("soloed", True)
                            result = self._solo_track(track_index, soloed)
                        elif command_type == "set_track_color":
                            track_index = params.get("track_index", 0)
                            color_index = params.get("color_index", 0)
                            result = self._set_track_color(track_index, color_index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
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
                            result = self._load_browser_item(track_index, item_uri)
                        elif command_type == "get_all_browser_items":
                            category_name = params.get("category_name", "audio_effects")
                            max_depth = params.get("max_depth", 10)
                            result = self._get_all_browser_items(category_name, max_depth)
                        elif command_type == "fuzzy_search_browser":
                            device_name = params.get("device_name", "")
                            category_name = params.get("category_name", "audio_effects")
                            threshold = params.get("threshold", 0.6)
                            result = self._fuzzy_search_browser(device_name, category_name, threshold)
                        elif command_type == "load_device_by_name":
                            track_index = params.get("track_index", 0)
                            device_name = params.get("device_name", "")
                            category_name = params.get("category_name", "audio_effects")
                            result = self._load_device_by_name(track_index, device_name, category_name)
                        elif command_type == "set_device_parameter":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            parameter_name = params.get("parameter_name", "")
                            value = params.get("value", 0.0)
                            result = self._set_device_parameter(track_index, device_index, parameter_name, value)
                        elif command_type == "get_track_routing_options":
                            track_index = params.get("track_index", 0)
                            result = self._get_track_routing_options(track_index)
                        elif command_type == "set_track_output_routing":
                            track_index = params.get("track_index", 0)
                            routing_type_name = params.get("routing_type_name", "")
                            channel_name = params.get("channel_name", None)
                            result = self._set_track_output_routing(track_index, routing_type_name, channel_name)
                        elif command_type == "set_track_input_routing":
                            track_index = params.get("track_index", 0)
                            routing_type_name = params.get("routing_type_name", "")
                            channel_name = params.get("channel_name", None)
                            result = self._set_track_input_routing(track_index, routing_type_name, channel_name)
                        elif command_type == "set_track_monitoring":
                            track_index = params.get("track_index", 0)
                            monitoring_state = params.get("monitoring_state", 1)
                            result = self._set_track_monitoring(track_index, monitoring_state)

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
                
                # Wait for the response with a timeout
                try:
                    task_response = response_queue.get(timeout=10.0)
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
    
    def _get_session_info(self):
        """Get information about the current session"""
        try:
            # Build list of all tracks with index and name
            tracks = []
            for idx, track in enumerate(self._song.tracks):
                tracks.append({
                    "index": idx,
                    "name": track.name
                })

            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "tracks": tracks,
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                }
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index):
        """Get information about a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
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
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": track.arm,
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise

    def _get_device_parameters(self, track_index, device_index):
        """Get all parameters for a device on a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")

            device = track.devices[device_index]

            # Get all parameters
            parameters = []
            for param_index, param in enumerate(device.parameters):
                parameters.append({
                    "index": param_index,
                    "name": param.name,
                    "value": param.value,
                    "min": param.min,
                    "max": param.max,
                    "is_enabled": param.is_enabled,
                    "value_string": param.str_for_value(param.value)
                })

            result = {
                "track_index": track_index,
                "device_index": device_index,
                "device_name": device.name,
                "parameters": parameters
            }
            return result
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, parameter_name, value):
        """Set a parameter value on a device"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")

            device = track.devices[device_index]

            # Find parameter by name (case-insensitive)
            param_found = None
            for param in device.parameters:
                if param.name.lower() == parameter_name.lower():
                    param_found = param
                    break

            if not param_found:
                raise ValueError(f"Parameter '{parameter_name}' not found on device '{device.name}'")

            # Clamp value to valid range
            clamped_value = max(param_found.min, min(param_found.max, value))

            # Set the value
            param_found.value = clamped_value

            result = {
                "track_index": track_index,
                "device_index": device_index,
                "device_name": device.name,
                "parameter_name": param_found.name,
                "old_value": param_found.value,
                "new_value": clamped_value,
                "value_string": param_found.str_for_value(clamped_value)
            }
            return result
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

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

    def _create_audio_track(self, index):
        """Create a new audio track at the specified index"""
        try:
            # Create the track
            self._song.create_audio_track(index)

            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]

            return {
                "index": new_track_index,
                "name": new_track.name
            }
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise

    def _delete_track(self, track_index):
        """Delete a track at the specified index"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track_name = self._song.tracks[track_index].name
            self._song.delete_track(track_index)

            return {
                "deleted_track": track_name,
                "deleted_index": track_index
            }
        except Exception as e:
            self.log_message("Error deleting track: " + str(e))
            raise

    def _duplicate_track(self, track_index):
        """Duplicate a track at the specified index"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            self._song.duplicate_track(track_index)

            # The duplicated track is inserted right after the original
            new_track_index = track_index + 1
            new_track = self._song.tracks[new_track_index]

            return {
                "original_index": track_index,
                "new_index": new_track_index,
                "new_track_name": new_track.name
            }
        except Exception as e:
            self.log_message("Error duplicating track: " + str(e))
            raise

    def _set_track_volume(self, track_index, volume):
        """Set the volume of a track (0.0 to 1.0)"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            # Clamp volume between 0.0 and 1.0
            volume = max(0.0, min(1.0, float(volume)))
            track.mixer_device.volume.value = volume

            return {
                "track_index": track_index,
                "volume": track.mixer_device.volume.value
            }
        except Exception as e:
            self.log_message("Error setting track volume: " + str(e))
            raise

    def _set_track_pan(self, track_index, pan):
        """Set the panning of a track (-1.0 to 1.0)"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            # Clamp pan between -1.0 and 1.0
            pan = max(-1.0, min(1.0, float(pan)))
            track.mixer_device.panning.value = pan

            return {
                "track_index": track_index,
                "pan": track.mixer_device.panning.value
            }
        except Exception as e:
            self.log_message("Error setting track pan: " + str(e))
            raise

    def _arm_track(self, track_index, armed):
        """Arm or disarm a track for recording"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            track.arm = bool(armed)

            return {
                "track_index": track_index,
                "armed": track.arm
            }
        except Exception as e:
            self.log_message("Error arming track: " + str(e))
            raise

    def _mute_track(self, track_index, muted):
        """Mute or unmute a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            track.mute = bool(muted)

            return {
                "track_index": track_index,
                "muted": track.mute
            }
        except Exception as e:
            self.log_message("Error muting track: " + str(e))
            raise

    def _solo_track(self, track_index, soloed):
        """Solo or unsolo a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            track.solo = bool(soloed)

            return {
                "track_index": track_index,
                "soloed": track.solo
            }
        except Exception as e:
            self.log_message("Error soloing track: " + str(e))
            raise

    def _set_track_color(self, track_index, color_index):
        """Set the color of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            track.color_index = int(color_index)

            return {
                "track_index": track_index,
                "color_index": track.color_index
            }
        except Exception as e:
            self.log_message("Error setting track color: " + str(e))
            raise

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
                if path_parts[0].lower() == "nstruments":
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
    
    
    
    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
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
    
    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI"""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item
            
            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None
            
            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                # Check all main categories
                categories = [
                    browser_or_item.instruments,
                    browser_or_item.sounds,
                    browser_or_item.drums,
                    browser_or_item.audio_effects,
                    browser_or_item.midi_effects
                ]
                
                for category in categories:
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

    def _get_all_browser_items(self, category_name, max_depth=10):
        """Get all loadable browser items from a category (audio_effects, midi_effects, instruments, drums, sounds)"""
        try:
            app = self.application()

            # Get the category root
            category_map = {
                "audio_effects": app.browser.audio_effects,
                "midi_effects": app.browser.midi_effects,
                "instruments": app.browser.instruments,
                "drums": app.browser.drums,
                "sounds": app.browser.sounds
            }

            category = category_map.get(category_name.lower())
            if not category:
                raise ValueError("Invalid category: {0}. Must be one of: audio_effects, midi_effects, instruments, drums, sounds".format(category_name))

            # Recursively collect all loadable items
            items = []
            self._collect_browser_items(category, items, max_depth, 0)

            return {
                "category": category_name,
                "count": len(items),
                "items": items
            }
        except Exception as e:
            self.log_message("Error getting browser items: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise

    def _collect_browser_items(self, browser_item, items, max_depth, current_depth):
        """Recursively collect all loadable items from a browser item"""
        try:
            if current_depth >= max_depth:
                return

            # If this item is loadable and is a device, add it
            if hasattr(browser_item, 'is_loadable') and browser_item.is_loadable and hasattr(browser_item, 'is_device') and browser_item.is_device:
                items.append({
                    "name": browser_item.name,
                    "uri": browser_item.uri
                })

            # Recursively check children
            if hasattr(browser_item, 'children') and browser_item.children:
                for child in browser_item.children:
                    self._collect_browser_items(child, items, max_depth, current_depth + 1)
        except Exception as e:
            self.log_message("Error collecting browser items: {0}".format(str(e)))

    def _fuzzy_search_browser(self, device_name, category_name, threshold=0.6):
        """Search for a device in the browser using fuzzy matching"""
        try:
            # Get all items from the category
            result = self._get_all_browser_items(category_name)
            items = result.get("items", [])

            if not items:
                return {
                    "found": False,
                    "message": "No items found in category {0}".format(category_name)
                }

            # Use fuzzy matching to find the best match
            best_match = None
            best_ratio = 0.0

            device_name_lower = device_name.lower()

            for item in items:
                item_name_lower = item["name"].lower()

                # Calculate similarity ratio
                ratio = difflib.SequenceMatcher(None, device_name_lower, item_name_lower).ratio()

                # Also check if the search term is a substring
                if device_name_lower in item_name_lower:
                    ratio = max(ratio, 0.8)  # Boost score for substring matches

                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = item

            # Check if we have a good enough match
            if best_match and best_ratio >= threshold:
                return {
                    "found": True,
                    "match": best_match,
                    "confidence": best_ratio,
                    "searched_count": len(items)
                }
            else:
                # Return top 5 matches for debugging
                all_matches = []
                for item in items:
                    item_name_lower = item["name"].lower()
                    ratio = difflib.SequenceMatcher(None, device_name_lower, item_name_lower).ratio()
                    if device_name_lower in item_name_lower:
                        ratio = max(ratio, 0.8)
                    all_matches.append((item, ratio))

                all_matches.sort(key=lambda x: x[1], reverse=True)
                top_matches = [{"name": m[0]["name"], "confidence": m[1]} for m in all_matches[:5]]

                return {
                    "found": False,
                    "message": "No match found with confidence >= {0}".format(threshold),
                    "best_confidence": best_ratio,
                    "top_matches": top_matches,
                    "searched_count": len(items)
                }
        except Exception as e:
            self.log_message("Error in fuzzy search: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise

    def _load_device_by_name(self, track_index, device_name, category_name):
        """Load a device onto a track by searching for it by name"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            # Search for the device
            search_result = self._fuzzy_search_browser(device_name, category_name)

            if not search_result.get("found"):
                return {
                    "loaded": False,
                    "error": search_result.get("message", "Device not found"),
                    "top_matches": search_result.get("top_matches", [])
                }

            # Get the matched item
            matched_item = search_result["match"]
            uri = matched_item["uri"]

            # Load the device using the URI
            track = self._song.tracks[track_index]
            app = self.application()

            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, uri)

            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(uri))

            # Select the track
            self._song.view.selected_track = track

            # Load the item
            app.browser.load_item(item)

            return {
                "loaded": True,
                "device_name": matched_item["name"],
                "searched_name": device_name,
                "confidence": search_result["confidence"],
                "track_name": track.name,
                "track_index": track_index,
                "uri": uri
            }
        except Exception as e:
            self.log_message("Error loading device by name: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise

    # Track Routing and Monitoring Methods

    def _get_track_routing_options(self, track_index):
        """Get available input and output routing options for a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Get available output routing types and channels
            output_types = []
            for routing_type in track.available_output_routing_types:
                output_types.append({
                    "display_name": routing_type.display_name,
                    "category": routing_type.category if hasattr(routing_type, 'category') else None
                })

            # Get current output routing
            current_output_type = track.output_routing_type.display_name if track.output_routing_type else None
            current_output_channel = track.output_routing_channel.display_name if track.output_routing_channel else None

            # Get available output channels for current type
            output_channels = []
            for channel in track.available_output_routing_channels:
                output_channels.append({
                    "display_name": channel.display_name
                })

            # Get available input routing types
            input_types = []
            for routing_type in track.available_input_routing_types:
                input_types.append({
                    "display_name": routing_type.display_name,
                    "category": routing_type.category if hasattr(routing_type, 'category') else None
                })

            # Get current input routing
            current_input_type = track.input_routing_type.display_name if track.input_routing_type else None
            current_input_channel = track.input_routing_channel.display_name if track.input_routing_channel else None

            # Get available input channels for current type
            input_channels = []
            for channel in track.available_input_routing_channels:
                input_channels.append({
                    "display_name": channel.display_name
                })

            # Get current monitoring state (0=In, 1=Auto, 2=Off)
            monitoring_state = track.current_monitoring_state
            monitoring_names = {0: "In", 1: "Auto", 2: "Off"}

            return {
                "track_index": track_index,
                "track_name": track.name,
                "output_routing": {
                    "current_type": current_output_type,
                    "current_channel": current_output_channel,
                    "available_types": output_types,
                    "available_channels": output_channels
                },
                "input_routing": {
                    "current_type": current_input_type,
                    "current_channel": current_input_channel,
                    "available_types": input_types,
                    "available_channels": input_channels
                },
                "monitoring": {
                    "current_state": monitoring_state,
                    "current_state_name": monitoring_names.get(monitoring_state, "Unknown")
                }
            }
        except Exception as e:
            self.log_message("Error getting track routing options: " + str(e))
            self.log_message(traceback.format_exc())
            raise

    def _set_track_output_routing(self, track_index, routing_type_name, channel_name=None):
        """Set the output routing of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Find the routing type by display name
            routing_type = None
            for rt in track.available_output_routing_types:
                if rt.display_name.lower() == routing_type_name.lower():
                    routing_type = rt
                    break

            if not routing_type:
                # List available types in error message
                available = [rt.display_name for rt in track.available_output_routing_types]
                raise ValueError("Output routing type '{0}' not found. Available: {1}".format(
                    routing_type_name, ", ".join(available)))

            # Set the output routing type
            track.output_routing_type = routing_type

            # If channel is specified, set it too
            if channel_name:
                # Need to refresh available channels after changing type
                channel = None
                for ch in track.available_output_routing_channels:
                    if ch.display_name.lower() == channel_name.lower():
                        channel = ch
                        break

                if channel:
                    track.output_routing_channel = channel

            return {
                "track_index": track_index,
                "track_name": track.name,
                "output_routing_type": track.output_routing_type.display_name,
                "output_routing_channel": track.output_routing_channel.display_name if track.output_routing_channel else None
            }
        except Exception as e:
            self.log_message("Error setting track output routing: " + str(e))
            self.log_message(traceback.format_exc())
            raise

    def _set_track_input_routing(self, track_index, routing_type_name, channel_name=None):
        """Set the input routing of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Find the routing type by display name
            routing_type = None
            for rt in track.available_input_routing_types:
                if rt.display_name.lower() == routing_type_name.lower():
                    routing_type = rt
                    break

            if not routing_type:
                # List available types in error message
                available = [rt.display_name for rt in track.available_input_routing_types]
                raise ValueError("Input routing type '{0}' not found. Available: {1}".format(
                    routing_type_name, ", ".join(available)))

            # Set the input routing type
            track.input_routing_type = routing_type

            # If channel is specified, set it too
            if channel_name:
                channel = None
                for ch in track.available_input_routing_channels:
                    if ch.display_name.lower() == channel_name.lower():
                        channel = ch
                        break

                if channel:
                    track.input_routing_channel = channel

            return {
                "track_index": track_index,
                "track_name": track.name,
                "input_routing_type": track.input_routing_type.display_name,
                "input_routing_channel": track.input_routing_channel.display_name if track.input_routing_channel else None
            }
        except Exception as e:
            self.log_message("Error setting track input routing: " + str(e))
            self.log_message(traceback.format_exc())
            raise

    def _set_track_monitoring(self, track_index, monitoring_state):
        """Set the monitoring state of a track (0=In, 1=Auto, 2=Off)"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Validate monitoring state
            if monitoring_state not in [0, 1, 2]:
                raise ValueError("Monitoring state must be 0 (In), 1 (Auto), or 2 (Off)")

            # Set the monitoring state
            track.current_monitoring_state = monitoring_state

            monitoring_names = {0: "In", 1: "Auto", 2: "Off"}

            return {
                "track_index": track_index,
                "track_name": track.name,
                "monitoring_state": track.current_monitoring_state,
                "monitoring_state_name": monitoring_names.get(track.current_monitoring_state, "Unknown")
            }
        except Exception as e:
            self.log_message("Error setting track monitoring: " + str(e))
            self.log_message(traceback.format_exc())
            raise

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
