import asyncio
import time
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from APP.Service.Roami_travel_planner.Roami_travel_planner_schema import (
    STTResponse,
    WebSocketRequest,
)
from APP.Service.Roami_Reassures.Roami_Reassures_router import (
    router as roami_reassures_router,
)
from APP.Service.Roami_travel_planner.Roami_travel_planner_router import (
    router as roami_travel_planner_router,
)
from APP.Service.voice_re_tigger.voice_retigger_router import (
    router as voice_retigger_router,
)
#from APP.Service.Recommand_trip.Recommand_router import router as recommand_trip_router
from APP.DB.MongoDB.mongobd import MongoDBSessionManager

app = FastAPI(title="Ackee AI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,  
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(roami_reassures_router, prefix="/v1", tags=["Roami Reassures"])
app.include_router(
    roami_travel_planner_router, prefix="/v1", tags=["Roami Travel Planner"]
)
app.include_router(voice_retigger_router, prefix="/v1", tags=["voice_retigger"])
#app.include_router(recommand_trip_router, prefix="/v1", tags=["Recommand Trip"])


@app.get("/")
def read_root():
    return {"message": "Welcome to Ackee AI API"}


@app.on_event("startup")
async def startup_event():
    MongoDBSessionManager()


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="Roami AI API",
        version="1.0.0",
        routes=app.routes,
    )

    # 1. Define the WebSocket Path
    openapi_schema["paths"]["/ws/tavel_planner"] = {
        "get": {
            "summary": "tavel_planner WebSocket",
            "description": "stream text and transcription data via WebSockets.",
            "tags": ["WebSockets"],
            "responses": {
                "101": {
                    "description": "Switching Protocols",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "incoming_message": {
                                        "$ref": "#/components/schemas/WebSocketRequest"
                                    },
                                    "outgoing_response": {
                                        "$ref": "#/components/schemas/STTResponse"
                                    },
                                },
                            }
                        }
                    },
                }
            },
        }
    }

    openapi_schema["paths"]["/ws/"] = {
        "get": {
            "summary": "reassures WebSocket",
            "description": "stream text , transcription,voice data via WebSockets.",
            "tags": ["WebSockets"],
            "responses": {
                "101": {
                    "description": "Switching Protocols",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "incoming_message": {
                                        "$ref": "#/components/schemas/WebSocketRequest"
                                    },
                                    "outgoing_response": {
                                        "$ref": "#/components/schemas/STTResponse"
                                    },
                                },
                            }
                        }
                    },
                }
            },
        }
    }

    # 2. Add the Pydantic models to the Components/Schemas section
    if "components" not in openapi_schema:
        openapi_schema["components"] = {"schemas": {}}

    openapi_schema["components"]["schemas"].update(
        {
            "WebSocketRequest": WebSocketRequest.model_json_schema(),
            "STTResponse": STTResponse.model_json_schema(),
        }
    )

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
