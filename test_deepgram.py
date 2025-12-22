import os
import websockets
import asyncio
import json
from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

async def test_raw_websocket():
    url = f"wss://api.deepgram.com/v1/listen?model=nova-3&encoding=linear16&sample_rate=16000"
    
    print(f"Connecting to: {url}")
    print(f"With key: {DEEPGRAM_API_KEY[:10]}...")
    
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as ws:
            print("RAW WEBSOCKET CONNECTED!")
            
            # Wait for any initial message
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                print(f"Received: {message}")
            except asyncio.TimeoutError:
                print("No initial message received")
            
            # Send some dummy audio
            dummy_audio = b'\x00' * 1024
            await ws.send(dummy_audio)
            print("Sent dummy audio")
            
            # Wait for response
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=2.0)
                print(f"Response: {response}")
            except asyncio.TimeoutError:
                print("No response received")
                
    except Exception as e:
        print(f"Connection error: {e}")
        import traceback
        traceback.print_exc()

asyncio.run(test_raw_websocket())
