import re  
from APP.modules.voice_pipeline.voice_pipeline import normalize_for_speech
from APP.modules.text_to_speech.tts_model import TTSservice
from ...DB.MongoDB.mongobd import MongoDBSessionManager
import os
import base64

tts_service = TTSservice()

def split_text_smart(text: str):
     sentences = re.split(r'(?<=[.!?])\s+', text)
     return [s.strip() for s in sentences if s.strip()]

async def get_voice_stream(session_id=None, user_id=None, message_id=None, message=None):
     try:
          if not message:
               db = MongoDBSessionManager()
               message = await db.get_ressure_AI_message(session_id, user_id, message_id)

          if not message:
               raise ValueError("Message not found")

          normalized_text = normalize_for_speech(message)
          sentences = split_text_smart(normalized_text)

          for sentence in sentences:
               audio_chunk = await tts_service.generate_speech(text=sentence)
               yield audio_chunk

     except Exception as e:
          print(f"Error in voice stream: {e}")
          raise 