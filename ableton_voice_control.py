import os
import asyncio
import json
import websockets
from dotenv import load_dotenv
from openai import AsyncOpenAI
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# LLM Model - can be overridden in .env file
LLM_MODEL = os.getenv("LLM_MODEL", "xiaomi/mimo-v2-flash:free")

# Audio settings
RATE = 16000
CHUNK = 1024
CHANNELS = 1
FORMAT = pyaudio.paInt16

class AbletonVoiceControl:
    def __init__(self):
        # Use OpenRouter with OpenAI-compatible API
        self.llm_client = AsyncOpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1"
        )
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
        """Process voice command with OpenRouter and execute via MCP (natural multi-turn loop)."""
        if not command:
            return

        print(f"\n🎤 Processing: {command}")

        try:
            # Convert MCP tools to OpenAI format
            openai_tools = []
            for tool in self.available_tools:
                openai_tools.append({
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

            # System prompt for Ableton control - optimized for speed
            system_prompt = """You are an Ableton Live controller. Execute user commands efficiently.

SPEED OPTIMIZATION - CRITICAL:
- Call MULTIPLE tools in a SINGLE response whenever possible
- If you need to add EQ to 3 tracks, call all 3 add_device tools at once
- Minimize round trips - batch related operations together
- Only use get_session_info() when you truly need track indices

TRACK OPERATIONS:
- When user mentions tracks by name, FIRST call get_session_info() to get indices
- Then perform ALL operations on those tracks in the SAME response

DEVICE OPERATIONS:
- To modify existing devices: get_track_info() → get_device_parameters() → set_device_parameter()
- Call these in sequence only when needed

RULES:
- NEVER guess track indices
- NEVER rename tracks unless explicitly asked
- NEVER load a new device if one already exists - control the existing one
- Be decisive - complete the task in as few turns as possible"""

            # Initial conversation messages
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": command}
            ]

            # Call OpenRouter
            print(f"🤖 Asking LLM ({LLM_MODEL})...")
            response = await self.llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                tools=openai_tools,
                tool_choice="auto",
                max_tokens=2000
            )

            # Multi-turn execution loop - continues until LLM indicates completion
            turn = 1

            while True:  # Trust the LLM to know when it's done
                print(f"\n{'='*60}")
                print(f"TURN {turn}")
                print(f"{'='*60}")

                # Get LLM response (first turn uses initial response)
                if turn == 1:
                    llm_response = response
                else:
                    # Ask LLM to continue
                    messages.append({
                        "role": "user",
                        "content": f"Continue with the next steps to complete: '{command}'. Call any remaining tools needed, or respond with text if complete."
                    })

                    llm_response = await self.llm_client.chat.completions.create(
                        model=LLM_MODEL,
                        messages=messages,
                        tools=openai_tools,
                        tool_choice="auto",
                        max_tokens=2000
                    )

                # Check if there are tool calls
                if llm_response.choices[0].message.tool_calls:
                    tool_calls = llm_response.choices[0].message.tool_calls
                    print(f"🔧 Executing {len(tool_calls)} tool(s) in parallel...")

                    # Add assistant message to history
                    messages.append(llm_response.choices[0].message)

                    # Execute all tool calls in PARALLEL for speed
                    async def execute_tool(tool_call):
                        func_name = tool_call.function.name
                        func_args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        print(f"  → {func_name}({func_args})")

                        try:
                            result = await self.mcp_session.call_tool(func_name, arguments=func_args)
                            if result.content:
                                result_text = result.content[0].text
                                print(f"    ✅ {result_text[:100]}..." if len(result_text) > 100 else f"    ✅ {result_text}")
                            else:
                                result_text = "Done"
                                print(f"    ✅ Done")
                        except Exception as tool_error:
                            result_text = f"Error: {tool_error}"
                            print(f"    ❌ {result_text}")

                        return {"tool_call_id": tool_call.id, "content": result_text}

                    # Run all tools in parallel
                    results = await asyncio.gather(*[execute_tool(tc) for tc in tool_calls])

                    # Add all tool results to conversation
                    for result in results:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": result["tool_call_id"],
                            "content": result["content"]
                        })

                    turn += 1

                else:
                    # No more tool calls - LLM says task is complete
                    final_response = llm_response.choices[0].message.content
                    if final_response:
                        print(f"\n💬 LLM: {final_response}")
                    print("\n✅ Command completed!\n")
                    break

        except Exception as e:
            print(f"❌ Error processing command: {e}")
            import traceback
            traceback.print_exc()

    async def run(self):
        """Main run loop - voice capture and command processing."""
        # Connect to MCP server first
        await self.connect_mcp()

        print(f"\n🎙️  Voice control ready! Say '{ACTIVATION_WORD}' to activate...\n")

        url = f"wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate={RATE}&channels={CHANNELS}&smart_format=true&interim_results=true&endpointing=300"

        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as ws:
            print("✅ Connected to Deepgram")

            # Give connection a moment to stabilize
            await asyncio.sleep(0.1)

            stream = self.audio.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

            print("✅ Microphone ready")
            # Test read to ensure mic is working
            try:
                test_data = stream.read(CHUNK, exception_on_overflow=False)
                print(f"✅ Audio test passed ({len(test_data)} bytes)\n")
            except Exception as e:
                print(f"❌ Microphone test failed: {e}")
                return

            silence_chunks_needed = int(SILENCE_DURATION * RATE / CHUNK)
            self.is_listening = True

            async def send_audio():
                """Send audio to Deepgram."""
                loop = asyncio.get_event_loop()
                try:
                    while self.is_listening:
                        # Run blocking read in executor
                        try:
                            data = await loop.run_in_executor(None, stream.read, CHUNK, False)
                            await ws.send(data)
                        except Exception as read_error:
                            await asyncio.sleep(0.01)
                            continue

                        # Silence detection during command recording
                        if self.is_recording_command:
                            audio_array = np.frombuffer(data, dtype=np.int16)
                            volume = np.abs(audio_array).mean()

                            if volume < SILENCE_THRESHOLD:
                                self.silent_chunks += 1
                            else:
                                self.silent_chunks = 0

                            # End command on silence
                            if self.silent_chunks >= silence_chunks_needed:
                                print("🔇 Silence detected, processing command...")
                                self.is_recording_command = False
                                self.is_activated = False

                                command = self.accumulated_transcript.strip()
                                if command:
                                    # Process command in background to avoid blocking audio stream
                                    asyncio.create_task(self.process_command(command))

                                self.accumulated_transcript = ""
                                self.silent_chunks = 0
                                print(f"\n🎙️  Listening for '{ACTIVATION_WORD}'...\n")
                except Exception as e:
                    print(f"❌ Audio send error: {e}")

            async def receive_transcripts():
                """Receive transcripts from Deepgram."""
                try:
                    async for message in ws:
                        result = json.loads(message)

                        if result.get("type") == "Results":
                            transcript = result["channel"]["alternatives"][0]["transcript"]
                            is_final = result.get("is_final", False)

                            if transcript and is_final:
                                # Wake word detection
                                if not self.is_activated and ACTIVATION_WORD.lower() in transcript.lower():
                                    print(f"✨ Activated! Say your command...")
                                    self.is_activated = True
                                    self.is_recording_command = True
                                    self.accumulated_transcript = ""
                                    self.silent_chunks = 0

                                # Command recording
                                elif self.is_activated and self.is_recording_command:
                                    self.accumulated_transcript += " " + transcript
                                    print(f"📝 {transcript}")
                except Exception as e:
                    print(f"❌ Transcript receive error: {e}")

            # Run both tasks concurrently
            try:
                await asyncio.gather(send_audio(), receive_transcripts())
            except KeyboardInterrupt:
                print("\n👋 Stopping voice control...")
            finally:
                self.is_listening = False
                stream.stop_stream()
                stream.close()
                self.audio.terminate()
                await self.disconnect_mcp()

if __name__ == "__main__":
    controller = AbletonVoiceControl()
    asyncio.run(controller.run())
