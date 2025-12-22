import os
import asyncio
import json
import websockets
from dotenv import load_dotenv
from groq import Groq
import pyaudio
import numpy as np
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# Configuration
ACTIVATION_WORD = "ableton"
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 2.0
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Audio settings
RATE = 16000
CHUNK = 1024
CHANNELS = 1
FORMAT = pyaudio.paInt16

class AbletonVoiceControl:
    def __init__(self):
        self.groq_client = Groq(api_key=GROQ_API_KEY)
        self.audio = pyaudio.PyAudio()
        self.is_listening = False
        self.is_activated = False
        self.is_recording_command = False
        self.accumulated_transcript = ""
        self.silent_chunks = 0
        self.mcp_session = None
        self.available_tools = []

    async def connect_mcp(self):
        """Connect to MCP server and load all available tools dynamically"""
        print("Connecting to Ableton MCP server...")

        server_params = StdioServerParameters(
            command="python3",
            args=[os.path.join(os.path.dirname(__file__), "ableton-mcp-extended/MCP_Server/server.py")]
        )

        # Store the context managers properly
        self.stdio_context = stdio_client(server_params)
        self.read, self.write = await self.stdio_context.__aenter__()

        self.session_context = ClientSession(self.read, self.write)
        self.mcp_session = await self.session_context.__aenter__()

        await self.mcp_session.initialize()

        # Get all available tools from the MCP server
        tools_result = await self.mcp_session.list_tools()
        self.available_tools = tools_result.tools

        print(f"✅ Connected! Loaded {len(self.available_tools)} tools from MCP server")
        print("Available commands:", ", ".join([tool.name for tool in self.available_tools[:10]]))
        if len(self.available_tools) > 10:
            print(f"  ... and {len(self.available_tools) - 10} more")

    async def disconnect_mcp(self):
        """Disconnect from MCP server"""
        if hasattr(self, 'session_context'):
            await self.session_context.__aexit__(None, None, None)
        if hasattr(self, 'stdio_context'):
            await self.stdio_context.__aexit__(None, None, None)

    async def process_command(self, command):
        """Process command with Groq and execute via MCP"""
        if not command:
            return

        print(f"Processing: {command}")

        try:
            # Convert MCP tools to Groq format
            groq_tools = []
            for tool in self.available_tools:
                groq_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or f"Execute {tool.name}",
                        "parameters": tool.inputSchema if hasattr(tool, 'inputSchema') and tool.inputSchema else {
                            "type": "object",
                            "properties": {}
                        }
                    }
                })

            # Run Groq in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": """You are an Ableton Live controller. Execute user commands using ONLY the available tools.

CRITICAL RULES:
1. When user says "audio track" → use create_audio_track (NOT create_midi_track)
2. When user says "MIDI track" → use create_midi_track (NOT create_audio_track)
3. When creating multiple items, call the function multiple times
4. If a requested action has no matching tool, do NOT call any tools
5. Track indices are 0-based integers
6. ALWAYS use the exact tool name that matches the user's request

Parameter types:
- index: integer (e.g., -1, 0, 1, 2)
- volume: float 0.0-1.0
- pan: float -1.0 to 1.0
- armed/muted/soloed: boolean"""},
                        {"role": "user", "content": command}
                    ],
                    tools=groq_tools,
                    tool_choice="auto",
                    max_tokens=1000
                )
            )

            if response.choices[0].message.tool_calls:
                for tool_call in response.choices[0].message.tool_calls:
                    func_name = tool_call.function.name
                    func_args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}

                    print(f"Executing: {func_name} with {func_args}")

                    # Call the MCP tool
                    result = await self.mcp_session.call_tool(func_name, arguments=func_args)
                    output = result.content[0].text if result.content else "Done"
                    print(f"Result: {output}")
            else:
                print("⚠️  Cannot perform this action - function not available in Ableton MCP server")
                print("    Available: create tracks, rename tracks, create/fire clips, set tempo, start/stop playback")
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    async def run(self):
        """Main run loop"""
        # Connect to MCP server first
        await self.connect_mcp()

        print(f"Starting voice control. Say '{ACTIVATION_WORD}' to activate...")

        url = f"wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate={RATE}&channels={CHANNELS}&smart_format=true&interim_results=true&endpointing=300"

        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as ws:
            print("Connected to Deepgram!")

            # Give connection a moment to stabilize
            await asyncio.sleep(0.1)

            stream = self.audio.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

            print(f"Audio stream opened. Testing microphone...")
            # Test read to ensure mic is working
            try:
                test_data = stream.read(CHUNK, exception_on_overflow=False)
                print(f"Microphone working! Read {len(test_data)} bytes")
            except Exception as e:
                print(f"Microphone test failed: {e}")
                return

            silence_chunks_needed = int(SILENCE_DURATION * RATE / CHUNK)
            self.is_listening = True

            async def send_audio():
                """Send audio to Deepgram"""
                loop = asyncio.get_event_loop()
                try:
                    print("Starting audio stream...")
                    while self.is_listening:
                        # Run blocking read in executor
                        try:
                            data = await loop.run_in_executor(None, stream.read, CHUNK, False)
                            await ws.send(data)
                        except Exception as read_error:
                            print(f"Audio read error: {read_error}")
                            await asyncio.sleep(0.01)
                            continue

                        if self.is_recording_command:
                            audio_array = np.frombuffer(data, dtype=np.int16)
                            volume = np.abs(audio_array).mean()

                            if volume < SILENCE_THRESHOLD:
                                self.silent_chunks += 1
                            else:
                                self.silent_chunks = 0

                            if self.silent_chunks >= silence_chunks_needed:
                                print("Silence detected")
                                self.is_recording_command = False
                                self.is_activated = False

                                command = self.accumulated_transcript.strip()
                                if command:
                                    # Process command in background to avoid blocking audio stream
                                    asyncio.create_task(self.process_command(command))

                                self.accumulated_transcript = ""
                                self.silent_chunks = 0
                                print(f"Listening for '{ACTIVATION_WORD}'...")
                except Exception as e:
                    print(f"Send error: {e}")

            async def receive_transcripts():
                """Receive transcripts from Deepgram"""
                try:
                    async for message in ws:
                        result = json.loads(message)

                        if result.get("type") == "Results":
                            transcript = result["channel"]["alternatives"][0]["transcript"]
                            is_final = result.get("is_final", False)

                            if transcript and is_final:
                                if not self.is_activated and ACTIVATION_WORD.lower() in transcript.lower():
                                    print(f"Activation: {transcript}")
                                    print("Listening for command...")
                                    self.is_activated = True
                                    self.is_recording_command = True
                                    self.accumulated_transcript = ""
                                    self.silent_chunks = 0
                                elif self.is_activated and self.is_recording_command:
                                    self.accumulated_transcript += " " + transcript
                                    print(f"Transcript: {transcript}")
                except Exception as e:
                    print(f"Receive error: {e}")

            try:
                await asyncio.gather(send_audio(), receive_transcripts())
            except KeyboardInterrupt:
                print("\nStopping...")
            finally:
                self.is_listening = False
                stream.stop_stream()
                stream.close()
                self.audio.terminate()
                await self.disconnect_mcp()

if __name__ == "__main__":
    controller = AbletonVoiceControl()
    asyncio.run(controller.run())
