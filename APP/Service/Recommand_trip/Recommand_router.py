'''import asyncio
from fastapi import APIRouter,HTTPException
from .Recommand_schema import CountryRecommendation, OutputSchema
from .Recommand_trip import Recommand_trip


router = APIRouter()
Recommand_trip_instance = Recommand_trip()

@router.get("/trip/recommendations", response_model=OutputSchema)
async def get_travel_recommendations(mood: str):
    try: 
        result = await Recommand_trip_instance.get_recommand_trip(mood)

        image_tasks = [Recommand_trip_instance.generate_image_url(prompt) for prompt in result["image_prompts"]]
        image_urls = await asyncio.gather(*image_tasks)

        return OutputSchema(
            mood_title=result["mood_title"],
            countries=[
                CountryRecommendation(name=c["name"], justification=c["justification"])
                for c in result["countries"]
            ],
            background_images=image_urls
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))'''