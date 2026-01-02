# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
from dataclasses import dataclass
from typing import Dict, Any, List, Union

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
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton: {str(e)}")
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
            "create_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            "set_track_output_routing", "set_track_input_routing", "set_track_monitoring"
        ]
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # For state-modifying commands, add a small delay to give Ableton time to process
            if is_modifying_command:
                import time
                time.sleep(0.1)  # 100ms delay
            
            # Set timeout based on command type
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
            
            # For state-modifying commands, add another small delay after receiving response
            if is_modifying_command:
                import time
                time.sleep(0.1)  # 100ms delay
            
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

# Create the MCP server
mcp = FastMCP("AbletonMCP")

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection
    
    if _ableton_connection is not None:
        try:
            # Test the connection with a simple ping
            # We'll try to send an empty message, which should fail if the connection is dead
            # but won't affect Ableton if it's alive
            _ableton_connection.sock.settimeout(1.0)
            _ableton_connection.sock.sendall(b'')
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
        # Try to connect up to 3 times with a short delay between attempts
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host="localhost", port=9877)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    
                    # Validate connection with a simple command
                    try:
                        # Get session info as a test
                        _ableton_connection.send_command("get_session_info")
                        logger.info("Connection validated successfully")
                        return _ableton_connection
                    except Exception as e:
                        logger.error(f"Connection validation failed: {str(e)}")
                        _ableton_connection.disconnect()
                        _ableton_connection = None
                        # Continue to next attempt
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None
            
            # Wait before trying again, but only if we have more attempts left
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
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session including all track names and indices"""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"

@mcp.tool()
def get_track_info(ctx: Context, track_index: int) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"

@mcp.tool()
def get_device_parameters(ctx: Context, track_index: int, device_index: int) -> str:
    """
    Get all parameters for a device on a track.

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device on the track (0 = first device)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", {
            "track_index": track_index,
            "device_index": device_index
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters from Ableton: {str(e)}")
        return f"Error getting device parameters: {str(e)}"

@mcp.tool()
def set_device_parameter(ctx: Context, track_index: int, device_index: int, parameter_name: str, value: float) -> str:
    """
    Set a parameter value on a device.

    Parameters:
    - track_index: The index of the track containing the device
    - device_index: The index of the device on the track (0 = first device)
    - parameter_name: The name of the parameter to set (case-insensitive)
    - value: The value to set (will be clamped to valid range)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_name": parameter_name,
            "value": value
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting device parameter in Ableton: {str(e)}")
        return f"Error setting device parameter: {str(e)}"

@mcp.tool()
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)

    Returns the created track index - use this index for subsequent operations on this track.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created MIDI track '{result.get('name', 'unknown')}' at index {result.get('index', -1)}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
def create_audio_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new audio track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)

    Returns the created track index - use this index for subsequent operations on this track.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created audio track '{result.get('name', 'unknown')}' at index {result.get('index', -1)}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
def delete_track(ctx: Context, track_index: int) -> str:
    """
    Delete a track from the Ableton session.

    Parameters:
    - track_index: The index of the track to delete
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_track", {"track_index": track_index})
        return f"Deleted track '{result.get('deleted_track', 'unknown')}' at index {result.get('deleted_index', track_index)}"
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"


@mcp.tool()
def duplicate_track(ctx: Context, track_index: int) -> str:
    """
    Duplicate a track in the Ableton session.

    Parameters:
    - track_index: The index of the track to duplicate
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("duplicate_track", {"track_index": track_index})
        return f"Duplicated track. New track: '{result.get('new_track_name', 'unknown')}' at index {result.get('new_index', -1)}"
    except Exception as e:
        logger.error(f"Error duplicating track: {str(e)}")
        return f"Error duplicating track: {str(e)}"


@mcp.tool()
def set_track_volume(ctx: Context, track_index: int, volume: float) -> str:
    """
    Set the volume of a track.

    Parameters:
    - track_index: The index of the track
    - volume: Volume level (0.0 to 1.0)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_volume", {"track_index": track_index, "volume": volume})
        return f"Set track {track_index} volume to {result.get('volume', volume):.2f}"
    except Exception as e:
        logger.error(f"Error setting track volume: {str(e)}")
        return f"Error setting track volume: {str(e)}"


@mcp.tool()
def set_track_pan(ctx: Context, track_index: int, pan: float) -> str:
    """
    Set the panning of a track.

    Parameters:
    - track_index: The index of the track
    - pan: Pan position (-1.0 = left, 0.0 = center, 1.0 = right)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_pan", {"track_index": track_index, "pan": pan})
        return f"Set track {track_index} pan to {result.get('pan', pan):.2f}"
    except Exception as e:
        logger.error(f"Error setting track pan: {str(e)}")
        return f"Error setting track pan: {str(e)}"


@mcp.tool()
def arm_track(ctx: Context, track_index: int, armed: bool = True) -> str:
    """
    Arm or disarm a track for recording.

    Parameters:
    - track_index: The index of the track
    - armed: True to arm, False to disarm
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("arm_track", {"track_index": track_index, "armed": armed})
        status = "armed" if result.get('armed', armed) else "disarmed"
        return f"Track {track_index} {status}"
    except Exception as e:
        logger.error(f"Error arming track: {str(e)}")
        return f"Error arming track: {str(e)}"


@mcp.tool()
def mute_track(ctx: Context, track_index: int, muted: bool = True) -> str:
    """
    Mute or unmute a track.

    Parameters:
    - track_index: The index of the track
    - muted: True to mute, False to unmute
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("mute_track", {"track_index": track_index, "muted": muted})
        status = "muted" if result.get('muted', muted) else "unmuted"
        return f"Track {track_index} {status}"
    except Exception as e:
        logger.error(f"Error muting track: {str(e)}")
        return f"Error muting track: {str(e)}"


@mcp.tool()
def solo_track(ctx: Context, track_index: int, soloed: bool = True) -> str:
    """
    Solo or unsolo a track.

    Parameters:
    - track_index: The index of the track
    - soloed: True to solo, False to unsolo
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("solo_track", {"track_index": track_index, "soloed": soloed})
        status = "soloed" if result.get('soloed', soloed) else "unsoloed"
        return f"Track {track_index} {status}"
    except Exception as e:
        logger.error(f"Error soloing track: {str(e)}")
        return f"Error soloing track: {str(e)}"


@mcp.tool()
def set_track_color(ctx: Context, track_index: int, color_index: int) -> str:
    """
    Set the color of a track.

    Parameters:
    - track_index: The index of the track
    - color_index: Color index (0-69 in Ableton Live)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_color", {"track_index": track_index, "color_index": color_index})
        return f"Set track {track_index} color to index {result.get('color_index', color_index)}"
    except Exception as e:
        logger.error(f"Error setting track color: {str(e)}")
        return f"Error setting track color: {str(e)}"


@mcp.tool()
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"


@mcp.tool()
def get_track_routing_options(ctx: Context, track_index: int) -> str:
    """
    Get available input and output routing options for a track.

    Parameters:
    - track_index: The index of the track to get routing options for

    Returns information about:
    - Current and available output routing types and channels
    - Current and available input routing types and channels
    - Current monitoring state (In/Auto/Off)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_routing_options", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track routing options: {str(e)}")
        return f"Error getting track routing options: {str(e)}"


@mcp.tool()
def set_track_output_routing(ctx: Context, track_index: int, routing_type_name: str, channel_name: str = None) -> str:
    """
    Set the output routing of a track.

    Parameters:
    - track_index: The index of the track to route
    - routing_type_name: The name of the routing destination (e.g., "Master", track name, "Sends Only")
    - channel_name: Optional channel name for the routing (e.g., "Track In")

    Use get_track_routing_options to see available routing types for a track.
    To route a track to another track (for grouping), use the target track's name as routing_type_name.
    """
    try:
        ableton = get_ableton_connection()
        params = {
            "track_index": track_index,
            "routing_type_name": routing_type_name
        }
        if channel_name:
            params["channel_name"] = channel_name
        result = ableton.send_command("set_track_output_routing", params)
        return f"Set track {track_index} output routing to: {result.get('output_routing_type', routing_type_name)}"
    except Exception as e:
        logger.error(f"Error setting track output routing: {str(e)}")
        return f"Error setting track output routing: {str(e)}"


@mcp.tool()
def set_track_input_routing(ctx: Context, track_index: int, routing_type_name: str, channel_name: str = None) -> str:
    """
    Set the input routing of a track.

    Parameters:
    - track_index: The index of the track to configure
    - routing_type_name: The name of the input source (e.g., "No Input", "Ext. In", track name)
    - channel_name: Optional channel name for the input

    Use get_track_routing_options to see available input routing types for a track.
    Use "No Input" for group/bus tracks that only receive audio from other tracks.
    """
    try:
        ableton = get_ableton_connection()
        params = {
            "track_index": track_index,
            "routing_type_name": routing_type_name
        }
        if channel_name:
            params["channel_name"] = channel_name
        result = ableton.send_command("set_track_input_routing", params)
        return f"Set track {track_index} input routing to: {result.get('input_routing_type', routing_type_name)}"
    except Exception as e:
        logger.error(f"Error setting track input routing: {str(e)}")
        return f"Error setting track input routing: {str(e)}"


@mcp.tool()
def set_track_monitoring(ctx: Context, track_index: int, monitoring_state: int) -> str:
    """
    Set the monitoring state of a track.

    Parameters:
    - track_index: The index of the track to configure
    - monitoring_state: 0 = In (always monitor), 1 = Auto (monitor when armed), 2 = Off (never monitor)

    For group/bus tracks that receive audio from other tracks, use monitoring_state=0 (In) to hear the audio.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_monitoring", {
            "track_index": track_index,
            "monitoring_state": monitoring_state
        })
        state_name = result.get('monitoring_state_name', ['In', 'Auto', 'Off'][monitoring_state])
        return f"Set track {track_index} monitoring to: {state_name}"
    except Exception as e:
        logger.error(f"Error setting track monitoring: {str(e)}")
        return f"Error setting track monitoring: {str(e)}"


@mcp.tool()
def create_track_group(ctx: Context, group_name: str, track_indices: List[int]) -> str:
    """
    Create a group from existing tracks using routing (classic bus/group technique).

    This creates an audio track as a "group bus" and routes all specified tracks to it.
    Since Ableton's API doesn't support native group tracks or track reordering,
    this uses the classic routing method to achieve the same audio result.

    Parameters:
    - group_name: Name for the group track (e.g., "Drums Bus", "Vocals Group")
    - track_indices: List of track indices to route to the group

    The function will:
    1. Create an audio track named with group_name
    2. Set the group track's input to "No Input"
    3. Set the group track's monitoring to "In"
    4. Route all specified tracks' outputs to the group track

    Note: Tracks cannot be physically moved adjacent to the group via API,
    but they will all be routed to the group track for audio summing.

    For creating NEW tracks that are grouped together, use create_grouped_tracks instead.
    """
    try:
        ableton = get_ableton_connection()

        # Step 1: Create the group audio track at the end
        create_result = ableton.send_command("create_audio_track", {"index": -1})
        group_track_index = create_result.get("index")

        # Step 2: Name the group track
        ableton.send_command("set_track_name", {
            "track_index": group_track_index,
            "name": group_name
        })

        # Step 3: Set input to "No Input"
        try:
            ableton.send_command("set_track_input_routing", {
                "track_index": group_track_index,
                "routing_type_name": "No Input"
            })
        except Exception as e:
            logger.warning(f"Could not set input to 'No Input': {str(e)}")

        # Step 4: Set monitoring to "In" (state 0)
        ableton.send_command("set_track_monitoring", {
            "track_index": group_track_index,
            "monitoring_state": 0
        })

        # Step 5: Route all tracks to the group
        routed_tracks = []
        failed_tracks = []

        for track_idx in track_indices:
            try:
                ableton.send_command("set_track_output_routing", {
                    "track_index": track_idx,
                    "routing_type_name": group_name
                })
                routed_tracks.append(track_idx)
            except Exception as e:
                logger.warning(f"Failed to route track {track_idx}: {str(e)}")
                failed_tracks.append(track_idx)

        # Build result message
        result_msg = f"Created group '{group_name}' at track index {group_track_index}. "
        result_msg += f"Routed {len(routed_tracks)} tracks to the group."

        if failed_tracks:
            result_msg += f" Failed to route tracks: {failed_tracks}"

        return result_msg
    except Exception as e:
        logger.error(f"Error creating track group: {str(e)}")
        return f"Error creating track group: {str(e)}"


@mcp.tool()
def create_grouped_tracks(
    ctx: Context,
    group_name: str,
    track_count: int,
    track_type: str = "midi",
    track_names: List[str] = None
) -> str:
    """
    Create new tracks and group them together in one operation.

    This creates the group track FIRST, then creates the member tracks sequentially,
    so they appear in order: [Group Track] → [Track 1] → [Track 2] → ...

    Parameters:
    - group_name: Name for the group track (e.g., "Drums", "Synths", "Vocals")
    - track_count: Number of tracks to create within the group
    - track_type: Type of tracks to create - "midi" or "audio" (default: "midi")
    - track_names: Optional list of names for each track (e.g., ["Kick", "Snare", "HiHat"])
                   If not provided, tracks are named "{group_name} 1", "{group_name} 2", etc.

    The function will:
    1. Create the group audio track first (with "No Input" and monitoring "In")
    2. Create each member track sequentially after the group
    3. Name each track appropriately
    4. Route all member tracks to the group track

    Example:
        create_grouped_tracks("Drums", 3, "midi", ["Kick", "Snare", "HiHat"])

        Results in:
        - Drums (group track, audio)
        - Kick (midi, routed to Drums)
        - Snare (midi, routed to Drums)
        - HiHat (midi, routed to Drums)
    """
    try:
        ableton = get_ableton_connection()

        # Validate track_type
        track_type = track_type.lower()
        if track_type not in ["midi", "audio"]:
            return f"Error: track_type must be 'midi' or 'audio', got '{track_type}'"

        # Step 1: Create the group audio track at the end
        create_result = ableton.send_command("create_audio_track", {"index": -1})
        group_track_index = create_result.get("index")

        # Step 2: Name the group track
        ableton.send_command("set_track_name", {
            "track_index": group_track_index,
            "name": group_name
        })

        # Step 3: Set input to "No Input"
        try:
            ableton.send_command("set_track_input_routing", {
                "track_index": group_track_index,
                "routing_type_name": "No Input"
            })
        except Exception as e:
            logger.warning(f"Could not set input to 'No Input': {str(e)}")

        # Step 4: Set monitoring to "In" (state 0)
        ableton.send_command("set_track_monitoring", {
            "track_index": group_track_index,
            "monitoring_state": 0
        })

        # Step 5: Create member tracks sequentially (they'll appear after the group)
        created_tracks = []

        for i in range(track_count):
            # Create the track at the end (will be after group and previous tracks)
            if track_type == "midi":
                track_result = ableton.send_command("create_midi_track", {"index": -1})
            else:
                track_result = ableton.send_command("create_audio_track", {"index": -1})

            new_track_index = track_result.get("index")

            # Determine track name
            if track_names and i < len(track_names):
                track_name = track_names[i]
            else:
                track_name = f"{group_name} {i + 1}"

            # Name the track
            ableton.send_command("set_track_name", {
                "track_index": new_track_index,
                "name": track_name
            })

            # Route to the group
            try:
                ableton.send_command("set_track_output_routing", {
                    "track_index": new_track_index,
                    "routing_type_name": group_name
                })
            except Exception as e:
                logger.warning(f"Failed to route track {new_track_index} to group: {str(e)}")

            created_tracks.append({
                "index": new_track_index,
                "name": track_name
            })

        # Build result message
        track_names_str = ", ".join([t["name"] for t in created_tracks])
        result_msg = f"Created group '{group_name}' (index {group_track_index}) with {track_count} {track_type} tracks: {track_names_str}. "
        result_msg += f"All tracks routed to '{group_name}'."

        return result_msg
    except Exception as e:
        logger.error(f"Error creating grouped tracks: {str(e)}")
        return f"Error creating grouped tracks: {str(e)}"


@mcp.tool()
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.
    
    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
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
def add_notes_to_clip(
    ctx: Context, 
    track_index: int, 
    clip_index: int, 
    notes: List[Dict[str, Union[int, float, bool]]]
) -> str:
    """
    Add MIDI notes to a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
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
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
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
def set_tempo(ctx: Context, tempo: float) -> str:
    """
    Set the tempo of the Ableton session.
    
    Parameters:
    - tempo: The new tempo in BPM
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str) -> str:
    """
    Load an instrument or effect onto a track using its URI.
    
    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
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
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Start playing a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
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
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Stop playing a clip.
    
    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
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
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session."""
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
def get_browser_tree(ctx: Context, category_type: str = "all") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.
    
    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
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
def get_browser_items_at_path(ctx: Context, path: str) -> str:
    """
    Get browser items at a specific path in Ableton's browser.
    
    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
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
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str) -> str:
    """
    Load a drum rack and then load a specific drum kit into it.
    
    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
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


@mcp.tool()
def get_all_browser_items(ctx: Context, category_name: str = "audio_effects", max_depth: int = 10) -> str:
    """
    Get all loadable browser items from a category.

    Parameters:
    - category_name: Category to search (audio_effects, midi_effects, instruments, drums, sounds)
    - max_depth: Maximum depth to search (default: 10)

    Returns a list of all loadable devices/instruments in the specified category.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_all_browser_items", {
            "category_name": category_name,
            "max_depth": max_depth
        })

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting browser items: {str(e)}")
        return f"Error getting browser items: {str(e)}"


@mcp.tool()
def fuzzy_search_browser(ctx: Context, device_name: str, category_name: str = "audio_effects", threshold: float = 0.6) -> str:
    """
    Search for a device in the browser using fuzzy matching.

    Parameters:
    - device_name: Name of the device to search for (e.g., "compressor", "EQ Eight")
    - category_name: Category to search in (audio_effects, midi_effects, instruments, drums, sounds)
    - threshold: Minimum confidence score for a match (0.0-1.0, default: 0.6)

    Returns the best match with confidence score, or top 5 suggestions if no match found.
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fuzzy_search_browser", {
            "device_name": device_name,
            "category_name": category_name,
            "threshold": threshold
        })

        if result.get("found"):
            return f"Found: {result['match']['name']} (confidence: {result['confidence']:.2f})"
        else:
            top_matches = result.get("top_matches", [])
            if top_matches:
                matches_str = "\n".join([f"  - {m['name']} (confidence: {m['confidence']:.2f})" for m in top_matches])
                return f"No match found. Top suggestions:\n{matches_str}"
            else:
                return f"No match found for '{device_name}' in {category_name}"
    except Exception as e:
        logger.error(f"Error searching browser: {str(e)}")
        return f"Error searching browser: {str(e)}"


@mcp.tool()
def load_device_by_name(ctx: Context, track_index: int, device_name: str, category_name: str = "audio_effects") -> str:
    """
    Load a device onto a track by searching for it by name (with fuzzy matching).

    Parameters:
    - track_index: Index of the track to load the device onto
    - device_name: Name of the device to load (e.g., "compressor", "EQ Eight", "glue compressor")
    - category_name: Category to search in (audio_effects, midi_effects, instruments, drums, sounds)

    This is the preferred way to load devices as it uses fuzzy matching to find the closest match.
    Examples:
    - "compressor" might match "Compressor"
    - "glue compressor" will match "Glue Compressor" (not just "Compressor")
    - "EQ" might match "EQ Eight" or "EQ Three"
    - "eq eight" will specifically match "EQ Eight"
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_device_by_name", {
            "track_index": track_index,
            "device_name": device_name,
            "category_name": category_name
        })

        if result.get("loaded"):
            return f"Loaded '{result['device_name']}' on track {track_index} (confidence: {result['confidence']:.2f})"
        else:
            error = result.get("error", "Unknown error")
            top_matches = result.get("top_matches", [])
            if top_matches:
                matches_str = "\n".join([f"  - {m['name']} (confidence: {m['confidence']:.2f})" for m in top_matches])
                return f"Failed to load device: {error}\nDid you mean:\n{matches_str}"
            else:
                return f"Failed to load device: {error}"
    except Exception as e:
        logger.error(f"Error loading device by name: {str(e)}")
        return f"Error loading device by name: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()