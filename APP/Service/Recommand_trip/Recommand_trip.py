'''from openai import AsyncOpenAI
from ...config.config import settings
from ...prompt.prompt import recommendations_trip_system_prompt
from .Recommand_schema import OutputSchema


class Recommand_trip:
    def __init__(self,):
        self.client = AsyncOpenAI(
            api_key = settings.OPENAI_API_KEY
        )

    async def get_recommand_trip(self, prompt: str):

        try:
            completions = await self.client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": recommendations_trip_system_prompt.format(output_schema=OutputSchema.model_json_schema())},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
            )
            response = completions.choices[0].message.content
            if not response:
                raise ValueError("Received empty response from OpenAI API")

            if response.startswith("```json"):
                response = response[8:-3].strip()

            return OutputSchema.parse_raw(response)
        except Exception as e:
            raise e

    async def generate_image_url(self, prompt: str) -> str:
        try:
            response = await self.client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            n=1,
            size="1024x1024",   
            quality="hd",
            style="natural"
        )

            content = response.data[0].url  
            if not content:
                raise ValueError("Received empty image URL from OpenAI API")
            return content
        except Exception as e:
            raise e'''