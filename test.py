import asyncio
from fastapi import WebSocket
import json
import base64

async def test_websocket():
    uri = "ws://localhost:8080/api/ws"  # Adjust your URL
    
    async with WebSocket.connect(uri) as websocket:
        # Test text message
        await websocket.send(json.dumps({
            "type": "text",
            "payload": "Hello, how are you?"
        }))
        
        while True:
            response = await websocket.recv()
            data = json.loads(response)
            print(data)
            if data.get("type") == "complete":
                break

