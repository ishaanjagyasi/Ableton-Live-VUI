"""
Ableton Voice Control V2 - Code Execution Pattern

Instead of multi-turn LLM tool calls, the LLM generates a Python script
that executes all MCP tool calls in a single sandbox run. This reduces
latency (fewer LLM round-trips) and token usage (compact function catalog
instead of 45 full JSON schemas).
"""

import os
import re
import asyncio
import json
import websockets
from dotenv import load_dotenv
from openai import AsyncOpenAI
import pyaudio
import numpy as np
import mido
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ableton_sandbox import AbletonSandbox, generate_function_catalog

load_dotenv()

# Configuration
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# MIDI push-to-talk config
MIDI_PORT_NAME = os.getenv("MIDI_PORT_NAME", "")  # empty = auto-detect Launchpad
MIDI_CHANNEL = int(os.getenv("MIDI_CHANNEL", "0"))  # MIDI channel 1 = 0 in mido
MIDI_NOTE = int(os.getenv("MIDI_NOTE", "-1"))  # -1 = any note on that channel

# LLM Model
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b:free")

# Local Ollama option
USE_OLLAMA = os.getenv("USE_OLLAMA", "false").lower() == "true"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:27b")

# Anthropic (Claude) option
USE_ANTHROPIC = os.getenv("USE_ANTHROPIC", "false").lower() == "true"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Audio settings
RATE = 16000
CHUNK = 1024
CHANNELS = 1
FORMAT = pyaudio.paInt16


class AbletonVoiceControlV2:
    def __init__(self):
        self.use_anthropic = USE_ANTHROPIC

        if USE_ANTHROPIC:
            import anthropic
            self.anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            self.model_name = ANTHROPIC_MODEL
            self.llm_client = None
        elif USE_OLLAMA:
            self.llm_client = AsyncOpenAI(
                api_key="ollama",
                base_url="http://localhost:11434/v1"
            )
            self.model_name = OLLAMA_MODEL
            self.anthropic_client = None
        else:
            self.llm_client = AsyncOpenAI(
                api_key=OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1"
            )
            self.model_name = LLM_MODEL
            self.anthropic_client = None

        self.audio = pyaudio.PyAudio()
        self.is_listening = False
        self.is_recording_command = False
        self.is_accepting_transcripts = False  # stays True briefly after button release
        self.accumulated_transcript = ""
        self.mcp_session = None
        self.sandbox = None
        self.function_catalog = ""
        self.midi_port = None

    async def connect_mcp(self):
        """Connect to MCP server, load tools, and initialize sandbox."""
        print("Connecting to Ableton MCP server...")

        server_params = StdioServerParameters(
            command="python3",
            args=[os.path.join(os.path.dirname(__file__), "ableton-mcp-extended/MCP_Server/server.py")]
        )

        self.stdio_context = stdio_client(server_params)
        self.read, self.write = await self.stdio_context.__aenter__()

        self.session_context = ClientSession(self.read, self.write)
        self.mcp_session = await self.session_context.__aenter__()

        await self.mcp_session.initialize()

        # Get all available tools
        tools_result = await self.mcp_session.list_tools()
        tools = tools_result.tools

        # Initialize sandbox with MCP session and tools
        self.sandbox = AbletonSandbox(self.mcp_session, tools)

        # Generate compact function catalog for system prompt
        self.function_catalog = generate_function_catalog(tools)

        if USE_ANTHROPIC:
            mode = "Anthropic (Claude)"
        elif USE_OLLAMA:
            mode = "Ollama (local)"
        else:
            mode = "OpenRouter"
        print(f"Connected! Loaded {len(tools)} tools as Python API")
        print(f"Using {mode} with model: {self.model_name}")
        print(f"Mode: Code Execution (single LLM round-trip)")

    async def disconnect_mcp(self):
        """Disconnect from MCP server."""
        if hasattr(self, 'session_context'):
            await self.session_context.__aexit__(None, None, None)
        if hasattr(self, 'stdio_context'):
            await self.stdio_context.__aexit__(None, None, None)

    def _build_system_prompt(self) -> str:
        """Build the system prompt with function catalog and examples."""
        return f"""You are an Ableton Live controller. When the user gives a command, write Python code to execute it.

The code runs in a sandbox with an `ableton` object. All methods are async -- always use `await`.
Every method returns a dict with the result data.

AVAILABLE FUNCTIONS:
{self.function_catalog}

RULES:
- Always `await` every ableton.xxx() call
- Use sequential awaits when order matters (create track before naming it)
- Use `asyncio.gather()` ONLY for truly independent operations (e.g., muting 4 tracks)
- `get_session_info()` returns {{"tracks": [{{"index": 0, "name": "Track 1", "type": "midi"}}, ...]}}
- Use get_session_info() to find track indices by name -- NEVER guess indices
- IMPORTANT: Some tools return JSON dicts, others return {{"raw": "text message"}}. Info tools like get_session_info() and get_track_info() return structured dicts. Action tools like create_midi_track(), set_track_name(), arm_track() return {{"raw": "..."}} with a status message.
- After creating tracks, call get_session_info() to get the updated track list with correct indices
- `json` module is available if needed
- Do NOT use `import` -- only `ableton`, `asyncio`, and `json` are available

USAGE PATTERNS:

Defaults:
- Default to create_audio_track() unless user specifically says "MIDI"
- Volume range: 0.0 to 1.0. Pan range: -1.0 (left) to 1.0 (right). Color index: 0-69
- Monitoring states: 0=In (always), 1=Auto (when armed), 2=Off (never)

Routing:
- To set input routing: FIRST call set_track_input_routing() to set the type (e.g. "Ext. In"), THEN call set_track_input_channel() separately to set the channel number. Do NOT try to set both in one call.
- Example: set input to channel 5: await ableton.set_track_input_routing(track_index=idx, routing_type_name="Ext. In") then await ableton.set_track_input_channel(track_index=idx, channel_name="5")
- Before setting routing, call get_track_routing_options(track_index=idx) to see available options
- To route a track to another track (for grouping), use the target track's name as routing_type_name
- For group/bus tracks: set input to "No Input" and monitoring to 0 (In) so audio passes through
- Channel names support fuzzy matching ("1" matches "1/2", "stereo" matches "Stereo")

Grouping:
- Prefer create_track_group(group_name, track_indices) for grouping existing tracks
- Prefer create_grouped_tracks(group_name, count, type, names) for creating new tracks already grouped
- These handle routing, naming, input, and monitoring automatically

Devices:
- To load a device: use load_device_by_name(track_index, device_name) -- it does fuzzy matching ("eq" matches "EQ Eight", "compressor" matches "Compressor")
- Specify category_name if needed: "audio_effects", "midi_effects", "instruments", "drums", "sounds"
- To modify a device: get_track_info() -> find device index -> get_device_parameters() -> set_device_parameter()
- parameter_name is case-insensitive in set_device_parameter()

Sends/Returns:
- IMPORTANT: Return tracks have a SEPARATE index space from regular tracks. Return track index 0 is NOT the same as track index 0.
- To load a device on a return track, use load_device_on_return_track(return_track_index, device_name) -- NOT load_device_by_name()
- load_device_by_name() is for regular tracks ONLY. load_device_on_return_track() is for return tracks ONLY.
- To control device params on return tracks: get_return_track_device_parameters() then set_return_track_device_parameter()
- get_device_parameters() and set_device_parameter() are for regular tracks ONLY
- For dry/wet, ALWAYS use parameter_name="Dry/Wet" -- the system handles name variations automatically (e.g. "Dry Wet" vs "Dry/Wet"). Do NOT search for the param name manually.
- Call get_return_tracks_info() to see existing return tracks before setting up sends
- Call get_track_sends() to see available sends and current levels for a track
- set_track_send() accepts letter ("A"), name ("Reverb"), or index ("0") as send_identifier
- create_return_track("Reverb") auto-assigns the next letter (A, B, C...)

Clips:
- Notes format for add_notes_to_clip: [{{"pitch": 60, "start_time": 0.0, "duration": 1.0, "velocity": 100, "mute": false}}]
- Clip length is in beats (4.0 = 1 bar at 4/4)

Browser:
- Use fuzzy_search_browser() to find devices by approximate name
- Use get_browser_tree() to explore browser categories
- Use load_device_by_name() as the preferred way to load devices (combines search + load)

EXAMPLES:

Example 1 - Find track by name and add device:
```python
session = await ableton.get_session_info()
drums_idx = None
for t in session["tracks"]:
    if "drum" in t["name"].lower():
        drums_idx = t["index"]
        break
if drums_idx is not None:
    await ableton.load_device_by_name(track_index=drums_idx, device_name="EQ Eight")
```

Example 2 - Create multiple tracks, name them, and arm:
```python
names = ["Kick", "Snare", "HiHat", "Bass"]
for n in names:
    await ableton.create_audio_track()
# Get updated session to find the new track indices
session = await ableton.get_session_info()
tracks = session["tracks"]
# The new tracks are the last N tracks
new_tracks = tracks[-len(names):]
for t, n in zip(new_tracks, names):
    await ableton.set_track_name(track_index=t["index"], name=n)
# Arm all new tracks (independent operations)
await asyncio.gather(*[ableton.arm_track(track_index=t["index"]) for t in new_tracks])
```

Example 3 - Set volume on all tracks to 80%:
```python
session = await ableton.get_session_info()
await asyncio.gather(*[
    ableton.set_track_volume(track_index=t["index"], volume=0.8)
    for t in session["tracks"]
])
```

Example 4 - Create grouped drum tracks with a return send:
```python
await ableton.create_grouped_tracks(group_name="Drums", track_count=3, track_type="midi", track_names=["Kick", "Snare", "HiHat"])
await ableton.create_return_track(name="Reverb")
```

RESPONSE FORMAT:
Return ONLY a Python code block. No explanation before or after. Just:
```python
# your code here
```"""

    async def _call_llm(self, system_prompt: str, messages: list, max_tokens: int = 4000) -> str:
        """Call LLM and return the response text. Handles both Anthropic and OpenAI backends."""
        if self.use_anthropic:
            # Anthropic API uses system as a separate parameter, not in messages
            user_messages = [m for m in messages if m["role"] != "system"]
            response = await self.anthropic_client.messages.create(
                model=self.model_name,
                system=system_prompt,
                messages=user_messages,
                max_tokens=max_tokens,
            )
            return response.content[0].text
        else:
            response = await self.llm_client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content

    def _extract_code(self, response_text: str) -> str:
        """Extract Python code from LLM response."""
        # Try fenced code block first
        match = re.search(r'```python\s*\n(.*?)```', response_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Try generic fenced block
        match = re.search(r'```\s*\n(.*?)```', response_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Assume entire response is code
        return response_text.strip()

    async def process_command(self, command):
        """Process voice command by having LLM generate Python code, then executing it."""
        if not command:
            return

        print(f"\n{'='*60}")
        print(f"COMMAND: {command}")
        print(f"{'='*60}")

        try:
            # Fetch current session state so LLM knows what exists
            session_context = ""
            try:
                session_result = await self.mcp_session.call_tool("get_session_info", arguments={})
                if session_result.content:
                    session_context += f"CURRENT SESSION STATE:\n{session_result.content[0].text}\n"
                returns_result = await self.mcp_session.call_tool("get_return_tracks_info", arguments={})
                if returns_result.content:
                    session_context += f"\nRETURN TRACKS:\n{returns_result.content[0].text}\n"
            except Exception:
                session_context = "(Could not fetch session info)\n"

            system_prompt = self._build_system_prompt()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{session_context}\nCOMMAND: {command}"}
            ]

            response_text = await self._call_llm(system_prompt, messages)
            code = self._extract_code(response_text)

            print(f"\n--- Generated Code ---")
            for i, line in enumerate(code.split('\n'), 1):
                print(f"  {i:2d} | {line}")
            print(f"--- End Code ---\n")

            # Execute in sandbox
            print(f"Executing...")
            result = await self.sandbox.execute(code)

            # If error, retry once with error context
            if not result.success:
                print(f"First attempt failed: {result.error}")
                print(f"Retrying with error feedback...")

                # Build a summary of what already succeeded so the retry doesn't redo it
                completed_summary = ""
                if result.call_log:
                    successful = [e for e in result.call_log if "error" not in e.get("result", "").lower()]
                    if successful:
                        completed_summary = "\n\nIMPORTANT: The following tool calls already SUCCEEDED before the error (do NOT repeat them):\n"
                        for entry in successful:
                            args_str = ", ".join(f"{k}={v!r}" for k, v in entry["args"].items()) if entry["args"] else ""
                            completed_summary += f"  - ableton.{entry['tool']}({args_str})\n"
                        completed_summary += "\nOnly write code for the steps that still need to happen. Use get_session_info() to find the current state."

                try:
                    messages.append({"role": "assistant", "content": response_text})
                    messages.append({
                        "role": "user",
                        "content": f"That code produced an error: {result.error}{completed_summary}\n\nPlease fix the code and try again. Return ONLY the corrected Python code block."
                    })

                    retry_text = await self._call_llm(system_prompt, messages)
                    retry_code = self._extract_code(retry_text)

                    print(f"\n--- Retry Code ---")
                    for i, line in enumerate(retry_code.split('\n'), 1):
                        print(f"  {i:2d} | {line}")
                    print(f"--- End Retry Code ---\n")

                    print(f"Executing retry...")
                    result = await self.sandbox.execute(retry_code)
                except Exception as retry_err:
                    print(f"Retry LLM call failed: {retry_err}")
                    # Keep the original result (from first attempt)

            # Print results
            self._print_summary(command, result)

        except Exception as e:
            print(f"\n{'='*60}")
            print(f"COMMAND: \"{command}\"")
            print(f"FAILED: {e}")
            print(f"{'='*60}\n")
            import traceback
            traceback.print_exc()

    def _print_summary(self, command: str, result):
        """Print execution summary."""
        tools_executed = len(result.call_log)

        if result.output:
            print(f"\n--- Output ---")
            print(result.output)
            print(f"--- End Output ---")

        print(f"\n{'='*60}")
        print(f"COMMAND: \"{command}\"")
        print(f"TOOLS CALLED: {tools_executed}")

        if result.call_log:
            for entry in result.call_log:
                status = "ERR" if "error" in entry.get("result", "").lower() else "OK"
                args_str = json.dumps(entry["args"]) if entry["args"] else ""
                print(f"  [{status}] {entry['tool']}({args_str})")

        if result.success:
            print(f"RESULT: Task completed successfully")
        else:
            print(f"RESULT: Task failed - {result.error}")
        print(f"{'='*60}\n")

    def _open_midi_port(self):
        """Open MIDI input port for push-to-talk trigger."""
        available = mido.get_input_names()
        if not available:
            print("ERROR: No MIDI input ports found!")
            print("Make sure your Launchpad is connected.")
            return False

        print(f"Available MIDI ports: {available}")

        # Auto-detect Launchpad or use configured port
        port_name = MIDI_PORT_NAME
        if not port_name:
            for name in available:
                if "launchpad" in name.lower():
                    port_name = name
                    break
            if not port_name:
                # Use first available port
                port_name = available[0]

        try:
            self.midi_port = mido.open_input(port_name)
            print(f"MIDI input: {port_name} (channel {MIDI_CHANNEL + 1})")
            if MIDI_NOTE >= 0:
                print(f"Trigger note: {MIDI_NOTE}")
            else:
                print(f"Trigger: any note on channel {MIDI_CHANNEL + 1}")
            return True
        except Exception as e:
            print(f"ERROR: Could not open MIDI port '{port_name}': {e}")
            return False

    async def run(self):
        """Main run loop - MIDI push-to-talk with voice capture."""
        await self.connect_mcp()

        # Open MIDI port
        if not self._open_midi_port():
            return

        print(f"\nVoice control ready! Hold MIDI button to speak, release to execute.\n")

        url = f"wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate={RATE}&channels={CHANNELS}&smart_format=true&interim_results=true&endpointing=300"

        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as ws:
            print("Connected to Deepgram")
            await asyncio.sleep(0.1)

            stream = self.audio.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

            print("Microphone ready")
            try:
                test_data = stream.read(CHUNK, exception_on_overflow=False)
                print(f"Audio test passed ({len(test_data)} bytes)\n")
            except Exception as e:
                print(f"Microphone test failed: {e}")
                return

            self.is_listening = True

            async def send_audio():
                """Send audio to Deepgram only while MIDI button is held."""
                loop = asyncio.get_event_loop()
                try:
                    while self.is_listening:
                        try:
                            data = await loop.run_in_executor(None, stream.read, CHUNK, False)
                            # Always send audio to keep Deepgram connection alive
                            if self.is_recording_command:
                                await ws.send(data)
                            else:
                                # Send silence to keep connection alive
                                silence = b'\x00' * len(data)
                                await ws.send(silence)
                        except Exception:
                            await asyncio.sleep(0.01)
                            continue
                except Exception as e:
                    print(f"Audio send error: {e}")

            async def receive_transcripts():
                """Receive transcripts from Deepgram."""
                try:
                    async for message in ws:
                        result = json.loads(message)
                        if result.get("type") == "Results":
                            transcript = result["channel"]["alternatives"][0]["transcript"]
                            is_final = result.get("is_final", False)

                            if transcript and is_final and self.is_accepting_transcripts:
                                self.accumulated_transcript += " " + transcript
                                print(f">> {transcript}")
                except Exception as e:
                    print(f"Transcript receive error: {e}")

            async def poll_midi():
                """Poll MIDI input for push-to-talk button."""
                loop = asyncio.get_event_loop()
                try:
                    while self.is_listening:
                        # Poll MIDI in executor to avoid blocking
                        msg = await loop.run_in_executor(None, self.midi_port.poll)
                        if msg is None:
                            await asyncio.sleep(0.01)
                            continue

                        # Check if it's on our channel
                        if hasattr(msg, 'channel') and msg.channel != MIDI_CHANNEL:
                            continue

                        # Check note filter
                        if MIDI_NOTE >= 0 and hasattr(msg, 'note') and msg.note != MIDI_NOTE:
                            continue

                        if msg.type == 'note_on' and msg.velocity > 0:
                            # Button pressed -- start recording
                            if not self.is_recording_command:
                                self.is_recording_command = True
                                self.is_accepting_transcripts = True
                                self.accumulated_transcript = ""
                                print("RECORDING... (speak now)")

                        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                            # Button released -- stop sending audio but keep accepting transcripts
                            if self.is_recording_command:
                                self.is_recording_command = False
                                print("RELEASED -- processing...")

                                # Wait for Deepgram to finish sending final transcripts
                                await asyncio.sleep(1.5)

                                # Now stop accepting transcripts
                                self.is_accepting_transcripts = False

                                command = self.accumulated_transcript.strip()
                                if command:
                                    asyncio.create_task(self.process_command(command))
                                else:
                                    print("No speech detected, try again.")
                                    print(f"\nReady -- hold MIDI button to speak.\n")

                                self.accumulated_transcript = ""
                except Exception as e:
                    print(f"MIDI poll error: {e}")

            print(f"Ready -- hold MIDI button to speak.\n")

            try:
                await asyncio.gather(send_audio(), receive_transcripts(), poll_midi())
            except KeyboardInterrupt:
                print("\nStopping voice control...")
            finally:
                self.is_listening = False
                stream.stop_stream()
                stream.close()
                self.audio.terminate()
                if self.midi_port:
                    self.midi_port.close()
                await self.disconnect_mcp()


if __name__ == "__main__":
    controller = AbletonVoiceControlV2()
    asyncio.run(controller.run())
