import asyncio
import os
import json
import sys
import pyaudio
from dotenv import load_dotenv

# --- LIBRARIES ---
from groq import Groq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# --- DEEPGRAM IMPORTS (Fail-Safe) ---
from deepgram import DeepgramClient
# We try to import specific classes, but fallback if the SDK version varies
try:
    from deepgram import LiveTranscriptionEvents, LiveOptions
except ImportError:
    try:
        from deepgram.clients.live.v1 import LiveTranscriptionEvents, LiveOptions
    except ImportError:
        # If imports fail completely, we will use raw strings/dicts in the code
        LiveTranscriptionEvents = None
        LiveOptions = None

# --- CONFIGURATION ---
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Points to the local clone of the repository
# We will add './ableton-mcp' to PYTHONPATH so we can run 'MCP_Server' as a module
ABLETON_REPO_PATH = os.path.abspath("./ableton-mcp")

class AbletonVoiceAssistant:
    def __init__(self):
        self.groq = Groq(api_key=GROQ_API_KEY)
        self.dg = DeepgramClient(DEEPGRAM_API_KEY)
        self.available_tools = []
        self.mcp_session = None
        self.audio_stream = None
        self.pyaudio_instance = None
        self.loop = None  # To store the main event loop

    async def start(self):
        print("🔌 Connecting to Ableton...")
        
        # Capture the main loop for thread-safe callbacks later
        self.loop = asyncio.get_running_loop()
        
        # Prepare the environment to run the MCP server module
        env = os.environ.copy()
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{ABLETON_REPO_PATH}{os.pathsep}{current_pythonpath}"

        # Run the module: equivalent to 'python -m MCP_Server.server'
        # We use sys.executable to ensure we use the same python environment running this script
        server_params = StdioServerParameters(
            command=sys.executable, 
            args=["-m", "MCP_Server.server"], 
            env=env
        )

        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    self.mcp_session = session
                    
                    # Initialize the MCP connection
                    await session.initialize()
                    
                    # Fetch available tools (Play, Stop, Create Track, etc.)
                    tools_result = await session.list_tools()
                    self.available_tools = tools_result.tools
                    
                    print(f"✅ Connected! Loaded {len(self.available_tools)} Ableton tools:")
                    for tool in self.available_tools:
                        print(f"   - {tool.name}")

                    # Fetch and print Session State immediately
                    print("\n📊 Fetching Initial Session State...")
                    initial_state = await self.get_session_context()
                    print(initial_state)
                    print("-" * 40)
                    
                    # Start the Voice Loop
                    await self.start_deepgram()
        except FileNotFoundError:
            print(f"❌ Error: Could not find the MCP Server at {ABLETON_REPO_PATH}")
            print("   Make sure you ran: git clone https://github.com/ahujasid/ableton-mcp.git")
        except Exception as e:
            print(f"❌ Connection Error: {e}")

    async def get_session_context(self):
        """
        Silently asks Ableton for the current state so the AI isn't blind.
        Improved: Specifically tries to fetch track names if basic info is insufficient.
        """
        if not self.mcp_session:
            return "Ableton not connected."

        try:
            # 1. Get Basic Info
            result = await self.mcp_session.call_tool("get_session_info", arguments={})
            
            if result.content and hasattr(result.content[0], 'text'):
                data = json.loads(result.content[0].text)
                
                summary = f"CURRENT ABLETON STATE:\n"
                summary += f"- Tempo: {data.get('tempo', 'Unknown')} BPM\n"
                track_count = data.get('track_count', 0)
                summary += f"- Total Tracks: {track_count}\n"
                
                # 2. Aggressively fetch track names if missing
                if 'tracks' in data:
                    summary += "- Track List:\n"
                    for i, track in enumerate(data['tracks']):
                        summary += f"  [{i}]: {track.get('name', 'Unknown')}\n"
                else:
                    # If basic info didn't give names, loop through tracks to get them
                    # This ensures the AI *knows* Track 0 is "drum"
                    summary += "- Track List (Fetched Detailed):\n"
                    # Limit to first 10 tracks to prevent lag on huge sessions
                    scan_limit = min(track_count, 10) 
                    
                    for i in range(scan_limit):
                        try:
                            # We call get_track_info for each track index
                            t_res = await self.mcp_session.call_tool("get_track_info", arguments={"track_index": i})
                            t_data = json.loads(t_res.content[0].text)
                            name = t_data.get('name', 'Unknown')
                            summary += f"  [{i}]: {name}\n"
                        except:
                            summary += f"  [{i}]: (Error fetching name)\n"
                        
                return summary
            
            return "Current Ableton State: Unknown (Could not parse response)"
            
        except Exception:
            return "Current Ableton State: Unknown (Context fetch failed)"

    async def start_deepgram(self):
        print("🎤 Listening... (Say 'Stop clips', 'Set tempo 120')")
        
        # ⚠️ OUTER LOOP: Keeps trying to reconnect if Deepgram times out
        while True:
            try:
                # 1. Setup WebSocket connection
                dg_connection = self.dg.listen.websocket.v("1")

                # 2. Define the callback (Thread-Safe)
                def on_message(self, result, **kwargs):
                    try:
                        sentence = result.channel.alternatives[0].transcript
                        if len(sentence) > 0 and result.is_final:
                            print(f"\n🗣️ You said: {sentence}")
                            
                            # Pass execution back to the main thread loop
                            if assistant.loop and assistant.loop.is_running():
                                asyncio.run_coroutine_threadsafe(
                                    assistant.process_command(sentence), 
                                    assistant.loop
                                )
                    except Exception:
                        pass

                # 3. Register Handler
                dg_connection.on(LiveTranscriptionEvents.Transcript if LiveTranscriptionEvents else "Results", on_message)

                # 4. Configure Options
                options_dict = {
                    "model": "nova-2",
                    "language": "en-US",
                    "smart_format": True,
                    "interim_results": False,
                    "encoding": "linear16",
                    "channels": 1,
                    "sample_rate": 16000,
                }
                
                options = LiveOptions(**options_dict) if LiveOptions else options_dict

                if dg_connection.start(options) is False:
                    print("❌ Failed to start Deepgram connection")
                    return

                # 5. Start Microphone (Manual Pyaudio)
                if self.pyaudio_instance is None:
                    self.pyaudio_instance = pyaudio.PyAudio()
                
                self.audio_stream = self.pyaudio_instance.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    frames_per_buffer=1024
                )

                print("🔴 Live Streaming Audio...")
                
                # 6. Audio Loop
                while True:
                    try:
                        data = self.audio_stream.read(1024, exception_on_overflow=False)
                        dg_connection.send(data)
                        await asyncio.sleep(0.001)
                    except KeyboardInterrupt:
                        print("👋 User stopped recording.")
                        return # Exit the whole function
                    except Exception as e:
                        print(f"⚠️ Audio Stream interrupted: {e}")
                        break # Break inner loop to trigger reconnect

            except Exception as e:
                print(f"❌ Connection Error (Reconnecting in 2s...): {e}")
                await asyncio.sleep(2)
            finally:
                # Cleanup local stream but keep loop running
                if self.audio_stream:
                    self.audio_stream.stop_stream()
                    self.audio_stream.close()
                    self.audio_stream = None

    async def process_command(self, user_text):
        """
        1. Get Context -> 2. Ask Groq -> 3. Execute Tool
        """
        # --- PHASE 1: CONTEXT ---
        # print("👀 Checking Ableton state...") # Optional debug
        session_context = await self.get_session_context()

        # --- PHASE 2: INTELLIGENCE ---
        groq_tools = [{
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.inputSchema
            }
        } for tool in self.available_tools]

        system_prompt = f"""
        You are an expert Ableton Live assistant.
        
        CONTEXT:
        {session_context}
        
        RULES:
        1. Use the Context above to verify tracks exist before modifying them.
        2. If the user asks for a track by name (e.g. "Bass"), try to match it to the list.
           - If names are missing in the Context, assume sensible indices based on the user's request history or guess logically (e.g. Bass is likely track 1 or 2).
        3. If the user asks to DELETE tracks, check if 'delete_track' is in the available tools. 
           - If 'delete_track' exists, use it.
           - If it DOES NOT exist (Groq error: "attempted to call tool... not in request.tools"), DO NOT call it. Instead, reply: "I cannot delete tracks directly with the current tools. Would you like me to mute or clear them instead?"
        4. If the user asks for something impossible (e.g. "Track 5" when only 2 exist), DO NOT call a tool. Instead, explain the error.
        5. If the user asks to "delete drum and bass", this likely means TWO separate actions: Delete "Drum" AND Delete "Bass". Call the tool twice or list multiple calls.
        """

        try:
            chat_completion = self.groq.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ],
                # ⚠️ Switch to "llama-4-scout" or "llama-3.1-8b-instant"
                model="llama-3.1-8b-instant",
                tools=groq_tools,
                tool_choice="auto"
            )
        except Exception as e:
            print(f"❌ Groq Error: {e}")
            return

        response_message = chat_completion.choices[0].message
        tool_calls = response_message.tool_calls

        # --- PHASE 3: EXECUTION ---
        if tool_calls:
            for tool_call in tool_calls:
                func_name = tool_call.function.name
                func_args = json.loads(tool_call.function.arguments)
                
                print(f"🎹 Action: {func_name} | Args: {func_args}")
                
                try:
                    result = await self.mcp_session.call_tool(func_name, arguments=func_args)
                    
                    # Handle empty results gracefully
                    output = result.content[0].text if result.content else "Done."
                    print(f"✅ Result: {output}")
                    
                except Exception as e:
                    print(f"❌ Execution Failed: {e}")
        else:
            # If the AI refused to act (e.g. "I can't do that"), print why
            print(f"🤖 AI: {response_message.content}")

if __name__ == "__main__":
    assistant = AbletonVoiceAssistant()
    try:
        asyncio.run(assistant.start())
    except KeyboardInterrupt:
        print("\n👋 Stopping...")