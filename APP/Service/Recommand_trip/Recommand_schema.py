'''from pydantic import BaseModel, Field
from typing import List

class CountryRecommendation(BaseModel):
    name: str = Field(..., description="Name of the recommended country")
    justification: str = Field(..., description="One-line justification for the recommendation")

class OutputSchema(BaseModel):
    mood_title: str = Field(..., description="Selected Mood")
    countries: List[CountryRecommendation] = Field(..., description="List of recommended countries with justifications")
    image_prompts: List[str] = Field(..., description="List of detailed image generation prompts for each country")'''