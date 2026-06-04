from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator
from dataclasses import dataclass

class RoamiTravelPlannerRequestSchema(BaseModel):
    type: Literal["text", "audio"]    
    user_input: Optional[str] = Field(
        None, 
        description="User input text"
    )
    @model_validator(mode="after")
    def validate_inputs(self):
        if self.type == "text" and not self.user_input:
            raise ValueError("user_input is required when type='text'")
        return self
    


@dataclass
class Context:
    """Runtime context containing user information"""
    user_id: str


class MemoryInfo(BaseModel):
    """Structure for memory content"""
    content: str
    memory_type: str



# The structure of the message sent TO the server
class WebSocketRequest(BaseModel):
    message_type: Literal["text"]
    payload: str
    user_id: str
    session_id: str

# The structure of the message sent FROM the server
class STTResponse(BaseModel):
    type: str = "stt_output"
    text: str
    latency: float
    timestamp: float
