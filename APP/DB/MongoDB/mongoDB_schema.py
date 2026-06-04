
from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator



class SessionCollectionSchema(BaseModel):
    ssession_id: str = Field(..., alias="SessionId")
    title: str
    user_id: str

    class Config():
        populate_by_name = True

