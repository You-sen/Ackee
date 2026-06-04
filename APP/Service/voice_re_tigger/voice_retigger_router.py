from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from .voice_retigger import get_voice_stream  
from .voice_retigger_schema import VoiceRetiggerRequest

router = APIRouter()

@router.post("/voice-retigger")
async def voice_retigger_endpoint(      
     body: VoiceRetiggerRequest
):
     try:
          gen = get_voice_stream(body.session_id, body.user_id, body.message_id, body.message)  
     except ValueError as e:
          raise HTTPException(status_code=404, detail=str(e))

     return StreamingResponse(
          gen,
          media_type="audio/mpeg",
          headers={"Content-Disposition": "inline; filename=speech.mp3"}
     )