from pydantic_settings import BaseSettings
from typing import Any

class Settings(BaseSettings):
    OPENAI_API_KEY: Any
    DATABASE_URL: str
    DATABASE_NAME: str
    COLLECTION_REASSURES_NAME: str
    COLLECTION_SESSION : str
    COLLECTION_USER : str
    COLLECTION_TRAVEL_NAME: str
    GOOGLE_API_KEY: str

    

    class Config:
        env_file = ".env"

settings = Settings()