
import base64
from .Roami_Reassures_schema import RoamiReassuresRequestSchema
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from .Roami_Reassures import Roami_Reassures
from ...DB.MongoDB.mongobd import MongoDBSessionManager as mongodb
from typing import Annotated, Optional
from ...modules.voice_pipeline.voice_pipeline import VoicePipeline
import json
import uuid 
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
mongodb_init = mongodb()
voice_pipeline_instance = VoicePipeline()
roami_reassures_instance = Roami_Reassures()


@router.websocket('/ws')
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info(f"✅ Client connected: {websocket.client}")
    
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            logger.info(f"📥 Received message type: {message_type}")
            
            is_new_session = False
            session_id = None
            
            try: 
                if message_type == "text":
                    text_input = data.get("payload")
                    user_id = data.get("user_id")
                    session_id = data.get("session_id")
                    user_mood = data.get("mood")
                    
                    if not session_id:
                        session_id = str(uuid.uuid4())
                        is_new_session = True
                    else:
                        is_new_session = False
                    
                    if not text_input:
                        raise ValueError("Text payload is required")
                    
                    logger.info(f"💬 Processing text: {text_input[:50]}...")
                    
                    # Collect full response for context
                    full_response = ""
                    async for text_chunk in roami_reassures_instance.get_response(
                        session_id=session_id,
                        user_input=text_input,
                        user_id=user_id,
                        user_mood=user_mood
                    ):
                        full_response += text_chunk
                        await websocket.send_json({
                            "type": "agent_text",
                            "text": text_chunk
                        })
                    
                    await websocket.send_json({"type": "complete"})
                    logger.info("✅ Text response complete")
                    
                    if is_new_session:
                        # Initialize default response BEFORE try block
                        respose = {
                            "title": "New Chat",
                            "subtitle": ""
                        }
                        
                        try:
                            # Pass both user input and AI response for better context
                            respose = await roami_reassures_instance.generate_session_title(
                                text_input, 
                                ai_response=full_response
                            )
                            logger.info(f"📝 Generated title: {respose['title']}")
                            
                            await websocket.send_json({
                                "type": "title",
                                "title": respose["title"],
                                "subtitle": respose["subtitle"],
                                "session_id": session_id
                            })
                        except Exception as e:
                            logger.error(f"Title generation error: {e}")
                            # respose already has default values
                            await websocket.send_json({
                                "type": "error",
                                "message": f"Title generation error: {str(e)}"
                            })
                        
                        try:
                            await mongodb_init.create_session(
                                user_id, 
                                session_id, 
                                respose["title"], 
                                respose["subtitle"], 
                                Type="reassures"
                            )
                            logger.info(f"💾 Session created: {session_id}")
                        except Exception as e:
                            logger.error(f"MongoDB error: {e}")
                            await websocket.send_json({
                                "type": "error",
                                "message": f"MongoDB error: {str(e)}"
                            })
                    
                elif message_type == "voice":
                    audio_data = data.get("payload")
                    user_id = data.get("user_id")
                    session_id = data.get("session_id")
                    user_mood = data.get("mood")

                    if not session_id:
                        session_id = str(uuid.uuid4())
                        is_new_session = True
                    
                    if not audio_data:
                        raise ValueError("Audio payload is required")
                    
                    logger.info("🎤 Processing voice message...")
                    
                    try:
                        audio_bytes = base64.b64decode(audio_data)
                        logger.info(f"🔊 Audio size: {len(audio_bytes)} bytes")
                    except Exception as e:
                        raise ValueError(f"Invalid base64 audio data: {str(e)}")
                    
                    # Store transcribed text AND AI response for title generation
                    transcribed_text = None
                    full_ai_response = ""
                    
                    async for event in voice_pipeline_instance.pipeline(
                        audio_data=audio_bytes,
                        response_function=roami_reassures_instance.get_response,
                        session_id=session_id,
                        user_id=user_id,
                        user_mood=user_mood
                    ):
                        await websocket.send_json(event)
                        
                        # Capture transcribed text (user's voice input)
                        if event.get("type") == "stt_output" and not transcribed_text:
                            transcribed_text = event.get("text", "")
                            logger.info(f"📝 Captured transcription: {transcribed_text[:50]}...")
                        
                        # Capture AI's text response
                        if event.get("type") == "agent_text":
                            full_ai_response += event.get("text", "")
                    
                    await websocket.send_json({"type": "complete"})
                    logger.info("✅ Voice response complete")
                    
                    if is_new_session:
                        # Initialize default response BEFORE try block
                        respose = {
                            "title": "Voice Chat",
                            "subtitle": ""
                        }
                        
                        try:
                            if transcribed_text:
                                # Pass both transcription and AI response for context
                                respose = await roami_reassures_instance.generate_session_title(
                                    transcribed_text,
                                    ai_response=full_ai_response
                                )
                                logger.info(f"📝 Generated title from voice: {respose['title']}")
                            else:
                                logger.warning("⚠️ No transcription found, using default title")
                        except Exception as e:
                            logger.error(f"Title generation error: {e}")
                            # respose already has default values
                            await websocket.send_json({
                                "type": "error",
                                "message": f"Title generation error: {str(e)}"
                            })
                        
                        # Send title to client
                        await websocket.send_json({
                            "type": "title",
                            "title": respose["title"],
                            "subtitle": respose["subtitle"],
                            "session_id": session_id
                        })
                        
                        try:
                            await mongodb_init.create_session(
                                user_id, 
                                session_id, 
                                respose["title"], 
                                respose["subtitle"], 
                                Type="reassures"
                            )
                            logger.info(f"💾 Voice session created: {session_id} with title: {respose['title']}")
                        except Exception as e:
                            logger.error(f"MongoDB error: {e}")
                            await websocket.send_json({
                                "type": "error",
                                "message": f"MongoDB error: {str(e)}"
                            })
                    
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Unknown message type: {message_type}"
                    })
                    
            except ValueError as ve:
                logger.error(f"❌ Validation error: {ve}")
                await websocket.send_json({
                    "type": "error",
                    "message": str(ve)
                })
            except Exception as msg_error:
                logger.error(f"❌ Processing error: {msg_error}")
                await websocket.send_json({
                    "type": "error",
                    "message": f"Processing error: {str(msg_error)}"
                })
                
    except WebSocketDisconnect:
        logger.info("🔌 Client disconnected normally")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        logger.info("👋 Closing connection")

'''
@router.post("/reassurances/")
async def roami_reassures_endpoint(
    request: RoamiReassuresRequestSchema,
    audio: Annotated[Optional[UploadFile], File()] = None
):
    """Non-streaming endpoint"""
    try:
        user_input = request.user_input
        user_id = getattr(request, 'user_id', 'default_user')
        session_id = getattr(request, 'session_id', 'roami_reassures_session')
        
        if not user_input:
            raise HTTPException(status_code=400, detail="user_input is required")
        
        # Collect all chunks
        response_text = ""
        async for chunk in roami_reassures_instance.get_response(
            session_id=session_id,
            user_input=user_input,
            user_id=user_id
        ):
            response_text += chunk
        
        return {"response": response_text}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reassurances/stream/")
async def roami_reassures_stream_endpoint(
    user_prompt: Annotated[str, Form()],
    user_id: Annotated[str, Form()] = "default_user",
    session_id: Annotated[Optional[str], Form()] = None,
    upload_file: Annotated[Optional[UploadFile], File()] = None
):
    """Streaming endpoint"""
    try:
        if not user_prompt:
            raise HTTPException(status_code=400, detail="user_prompt is required")
        
        # Generate session_id if not provided
        if not session_id:
            session_id = str(uuid.uuid4())
        
        return StreamingResponse(
            roami_reassures_instance.get_response(
                session_id=session_id,
                user_input=user_prompt,
                user_id=user_id
            ),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

'''
@router.get('/reassurances/chat_history')
async def roami_travel_planner_chat_history(
    session_id: str,
    user_id: str,
):
    """Retrieve chat history for a specific session"""
    try:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")

        history = await roami_reassures_instance.get_ressurance_chat_history(
            session_id=session_id,
            user_id=user_id,
        )

        messages = []
        for msg in history:
            messages.append({
                "type": msg["role"],       
                "content": msg["content"], 
                "created_at": msg.get("created_at"),
            })

        return {
            "session_id": session_id,
            "user_id": user_id,
            "chat_history": messages,
            "message_count": len(messages),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get('/reassurances/sessions')
async def list_active_sessions(user_id: str ,):
    """List all active sessions"""
    try:
        sessions = await roami_reassures_instance.list_all_active_sessions(user_id=user_id,Type='reassures')
        print(sessions)
        return {
            "sessions": sessions,
            "count": len(sessions)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete('/reassurances/sessions')
async def clear_session(session_id: str,user_id:str):
    """Clear/delete a specific session"""
    try:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        
        success = roami_reassures_instance.clear_session(session_id,user_id)
        await mongodb_init.delete_session(session_id,user_id)
        
        if success:
            return {
                "message": f"Session {session_id} cleared successfully",
                "session_id": session_id
            }
        else:
            raise HTTPException(status_code=404, detail="Session not found")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))