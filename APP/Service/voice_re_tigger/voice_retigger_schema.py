from pydantic import BaseModel, Field
from typing import Optional

class VoiceRetiggerRequest(BaseModel):
     session_id: Optional[str] = Field(None, description="The session ID") 
     user_id: Optional[str] = Field(None, description="The user ID")
     message_id: Optional[str] = Field(None, description="The message ID")
     message: Optional[str] = Field(None, description="The message")