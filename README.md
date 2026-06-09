# Ackee AI Backend (Roami AI Codebase)

## 1) What this project is

This repository is a FastAPI backend for an AI travel companion with:

- Real-time WebSocket chat for two experiences:
  - Reassurance assistant
  - Travel planner assistant
- Voice pipeline:
  - Speech to Text (OpenAI Whisper)
  - LLM response generation (OpenAI GPT models via LangChain/LangGraph)
  - Text to Speech (OpenAI TTS)
- MongoDB persistence for:
  - Sessions
  - Message history
  - User profile lookup
  - LangGraph checkpoints
  - Long-term memory vector store
- Tool-augmented responses using Google Places, Distance Matrix, and web search.

Branding note:
- Prompts currently instruct the bot to identify itself as Ackee.
- Some internal class/file names still contain Roami (intentional and safe).

## 2) Tech stack

- Python 3.11
- FastAPI + Uvicorn
- LangChain + LangGraph
- OpenAI APIs (GPT, Whisper, TTS, embeddings)
- MongoDB (Motor + PyMongo)
- Google Maps APIs (Places + Distance Matrix)
- Docker + Docker Compose
- ffmpeg (required for audio processing pipeline)

## 3) High-level architecture

### Application entrypoint
- `main.py`
  - Creates FastAPI app
  - Adds CORS middleware
  - Mounts routers under `/v1`
  - Customizes OpenAPI schema to include WS docs

### Core services
- `APP/Service/Roami_Reassures/`
  - Reassurance agent, tools, WebSocket/REST-ish routes
- `APP/Service/Roami_travel_planner/`
  - Travel planner agent, tools, WebSocket/REST-ish routes
- `APP/Service/voice_re_tigger/`
  - Re-synthesizes text messages into audio stream

### Data layer
- `APP/DB/MongoDB/mongobd.py`
  - Async session and message CRUD
- `APP/DB/long_term_memory/long_trem_memory.py`
  - MongoDBStore + embeddings-backed long-term memory
- `APP/DB/vectorStore/vectorStore.py`
  - Legacy/broken module (not used by main flow)

### Voice modules
- `APP/modules/speech_to_text/stt_model.py`
- `APP/modules/text_to_speech/tts_model.py`
- `APP/modules/voice_pipeline/voice_pipeline.py`

### Prompt definitions
- `APP/prompt/prompt.py`
  - System prompts and behavioral constraints

## 4) Current project structure

```text
Roami_AI-main/
|- main.py
|- Dockerfile
|- docker-compose.yml
|- requirements.txt
|- test.py
|- APP/
|  |- config/
|  |  |- config.py
|  |- DB/
|  |  |- MongoDB/
|  |  |  |- mongobd.py
|  |  |  |- mongoDB_schema.py
|  |  |- long_term_memory/
|  |  |  |- long_trem_memory.py
|  |  |- vectorStore/
|  |     |- vectorStore.py
|  |- modules/
|  |  |- speech_to_text/stt_model.py
|  |  |- text_to_speech/tts_model.py
|  |  |- voice_pipeline/voice_pipeline.py
|  |  |- MemoryExtractionMiddleware/MemoryExtractionMiddleware.py
|  |- prompt/
|  |  |- prompt.py
|  |- Service/
|     |- Roami_Reassures/
|     |- Roami_travel_planner/
|     |- voice_re_tigger/
|     |- Recommand_trip/ (currently commented out)
```

## 5) Features

### Real-time WebSocket chat
- Streaming text chunks from LLM
- Optional voice input/output in same WS session
- Session title auto-generation for new sessions

### Planner tools
- User profile fetch from DB (`get_user_info`)
- Web search (`web_search`)
- Place lookup (`google_place_search`)
- Distance lookup (`get_distance_to_place`)
- Batch place+distance (`get_multiple_places_and_distances`)

### Voice pipeline
- STT from base64 audio payload
- Live LLM token streaming
- Sentence-aware TTS chunk generation
- Text normalization for speech output

### Data persistence
- Session metadata (title, subtitle, type)
- Travel/reassures chat history
- LangGraph checkpoint recovery logic
- Long-term memory semantic retrieval

## 6) API surface

Base URL examples below assume local run on port `8080`.

### Root
- `GET /`
  - Returns welcome payload.

### WebSocket endpoints (main interaction paths)

Because routers are mounted with `/v1` prefix:

- Reassures WS: `ws://localhost:8080/v1/ws`
- Travel planner WS: `ws://localhost:8080/v1/ws/tavel_planner`

Note:
- `tavel_planner` is intentionally spelled this way in current code.

#### WebSocket request payload

```json
{
  "type": "text",
  "payload": "do you have any name?",
  "user_id": "u1",
  "session_id": "optional-session-id",
  "mood": "optional"
}
```

For voice:

```json
{
  "type": "voice",
  "payload": "<base64-encoded-audio>",
  "user_id": "u1",
  "session_id": "optional-session-id",
  "mood": "optional"
}
```

#### WebSocket event types returned

- `agent_text` (streamed token/chunk)
- `stt_output` (voice path transcription)
- `tts_audio` or `tts_output` (audio chunks, depending on pipeline route/event)
- `title` (new session title/subtitle)
- `complete`
- `error`

### HTTP endpoints

Mounted under `/v1`:

- `POST /v1/voice-retigger`
  - Returns `audio/mpeg` stream from stored/provided text

- `GET /v1/chat/history`
- `GET /v1/chat/sessions`
- `GET /v1/health`
- `DELETE /v1/chat/session`

- `GET /v1/reassurances/chat_history`
- `GET /v1/reassurances/sessions`
- `DELETE /v1/reassurances/sessions`

Note:
- Several REST endpoints for planner/reassurances request/stream are present but wrapped in triple-quoted blocks (disabled).

## 7) Runtime flow

### Text flow
1. Client sends WS JSON with `type="text"`.
2. Router calls corresponding service `get_response(...)`.
3. Service streams LLM chunks via LangGraph events.
4. Router forwards chunks as `agent_text`.
5. For new sessions:
   - title/subtitle generated
   - session document inserted in MongoDB
6. Chat messages persisted in travel message collection.

### Voice flow
1. Client sends WS JSON with `type="voice"` + base64 audio.
2. Router decodes audio and calls voice pipeline.
3. Pipeline performs:
   - STT transcription
   - LLM response streaming
   - TTS chunk generation from normalized text
4. Router forwards STT/text/audio events.
5. Session title generation and DB persistence happen for new sessions.

## 8) Environment variables

All required config fields are defined in `APP/config/config.py`.

### Required `.env` keys

- `OPENAI_API_KEY`
- `DATABASE_URL`
- `DATABASE_NAME`
- `COLLECTION_REASSURES_NAME`
- `COLLECTION_SESSION`
- `COLLECTION_USER`
- `COLLECTION_TRAVEL_NAME`
- `GOOGLE_API_KEY`

### Recommended `.env.example`

```env
# OpenAI
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx

# MongoDB
DATABASE_URL=mongodb://localhost:27017
DATABASE_NAME=ackee_ai

# Collections
COLLECTION_REASSURES_NAME=reassures_messages
COLLECTION_SESSION=sessions
COLLECTION_USER=users
COLLECTION_TRAVEL_NAME=travel_messages

# Google APIs
GOOGLE_API_KEY=AIzaxxxxxxxxxxxxxxxxxxxxxxxx
```

Google API notes:
- Enable Places API and Distance Matrix API in your Google Cloud project.

MongoDB notes:
- The code expects both session metadata and message collections to exist or be creatable.

## 9) Setup and run

### Option A: Local Python

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

### Option B: Docker Compose

```bash
docker compose up --build
```

Expected app URL:
- `http://localhost:8080`

### Running two cloned copies simultaneously

If you want both old and renamed stacks running together:

- Use different `container_name`
- Use different host ports

Example for second clone:

```yaml
container_name: Ackee_ai
ports:
  - "8081:8080"
```

This maps host `8081` to container `8080`.

## 10) WebSocket testing examples

Swagger UI does not execute WS sessions like HTTP "Try it out".
Use Postman WS, browser WS clients, or Python.

### Python quick test (`websockets`)

```python
import asyncio
import json
import websockets

async def main():
    uri = "ws://localhost:8080/v1/ws"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "text",
            "payload": "do you have any name?",
            "user_id": "u1"
        }))

        while True:
            msg = await ws.recv()
            print(msg)
            if '"type": "complete"' in msg:
                break

asyncio.run(main())
```

Travel planner endpoint:
- change URI to `ws://localhost:8080/v1/ws/tavel_planner`

## 11) Data model and collections (practical view)

Based on `mongobd.py` usage:

- Sessions (`COLLECTION_SESSION`)
  - `session_id`, `user_id`, `title`, `subtitle`, `type`, `created_at`, `last_activity`

- Reassures messages (`COLLECTION_REASSURES_NAME`)
  - legacy and read helpers present

- Travel messages (`COLLECTION_TRAVEL_NAME`)
  - `session_id`, `user_id`, `role`, `content`, `created_at`

- Users (`COLLECTION_USER`)
  - lookup by ObjectId in `get_user(...)`

- LangGraph checkpoint collections
  - `checkpoints`
  - `checkpoint_writes`

- Long-term memory collection
  - `long_term_memories`

## 12) Known issues and important caveats

1. `test.py` is not runnable as-is.
- Uses `fastapi.WebSocket.connect` (invalid client usage) and wrong route (`/api/ws`).

2. Planner WS path typo in code is `tavel_planner`.
- Clients must use that exact path unless code is renamed.

3. Some async calls are not awaited in delete session routes.
- `planner_instance.clear_session(...)` and `roami_reassures_instance.clear_session(...)` are async but called without `await` in routers.

4. OpenAPI title mismatch.
- `FastAPI` title is Ackee, but custom OpenAPI builder still sets title string to Roami.

5. `APP/DB/vectorStore/vectorStore.py` has invalid settings names.
- Appears unused in active runtime; do not rely on it without fixes.

6. Recommendation trip service is commented out and not mounted.

7. Reassures read helper names use mixed field casing (`SessionId`/`UserId`) that may not match inserted documents.

## 13) Troubleshooting

### `docker compose up --build` exits with code 1

Common causes:
- Missing required `.env` values
- Invalid/expired API keys
- MongoDB unreachable from container
- Host port already in use
- Existing container name conflict from another clone

Quick checks:

```bash
docker compose down
docker ps -a
```

If port conflict:
- Change host mapping, for example `"8081:8080"`.

If container name conflict:
- Use unique `container_name` per clone, or remove `container_name`.

### WebSocket route not working

Verify exact path and prefix:
- `/v1/ws`
- `/v1/ws/tavel_planner`

### Bot name response inconsistent between routes

Check `APP/prompt/prompt.py` for both system prompt blocks:
- `Roami_Reassures_system_prompt`
- `Roami_travel_planner_system_prompt`

Both should contain identity rule for Ackee.

## 14) Security and ops notes

- Never commit real `.env` secrets.
- Restrict CORS origins in production.
- Add API auth/rate limiting before public deployment.
- Add payload size limits for WS voice uploads.
- Add structured logging and request IDs for production debugging.

## 15) Suggested next improvements

- Add a real `.env.example` file to repository root.
- Fix `test.py` into a working WS client script.
- Normalize naming (`tavel_planner` -> `travel_planner`) with backward compatibility.
- Add unit/integration tests for:
  - WebSocket protocol
  - DB CRUD
  - Tool wrappers
  - Voice pipeline normalization
- Add CI workflow for lint/test/build.

---

