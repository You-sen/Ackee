import asyncio
import json
import logging
import os
from datetime import datetime
from typing import TypedDict, Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langchain.agents import create_agent
from langgraph.checkpoint.mongodb import MongoDBSaver
from pymongo import MongoClient
from ...config.config import settings
from .Roami_travel_planner_tools import get_all_tools
from ...prompt.prompt import Roami_travel_planner_system_prompt,title_generation_prompt
from ...DB.long_term_memory.long_trem_memory import LongTermMemory
from ...modules.MemoryExtractionMiddleware.MemoryExtractionMiddleware import MemoryExtractionMiddleware
from ...DB.MongoDB.mongobd import MongoDBSessionManager
from langchain_core.messages import HumanMessage, AIMessage

os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

LongTermMemory_instance = LongTermMemory()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Context(TypedDict):
    user_id: str


class RoamiTravelPlanner:
    """Main interface for Roami Travel Planner with streaming support"""
    
    def __init__(self):
        self.llm = ChatOpenAI(model="gpt-4o" , temperature=0.3, model_kwargs={"parallel_tool_calls": True})
        self.title_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.5)
        self.client = None
        self.checkpointer = None
        self.agent = None
        self.session_manager = None
        self._initialized = False
    
    async def _ensure_initialized(self):
        """Lazy initialization of async components"""
        if self._initialized:
            return
        
        self.client = MongoClient(settings.DATABASE_URL)
        
        self.session_manager = MongoDBSessionManager()
        
        self.checkpointer = MongoDBSaver(
            client=self.client,
            db_name=settings.DATABASE_NAME
        )
        
        long_term_store = LongTermMemory_instance.initialize_store()
        
        try:
        # If this is a LangChain BaseStore, it might not have 'asetup'
        # Or you might be calling it on the context manager instead of the store
            await LongTermMemory_instance.setup_store() 
            print("✅ Long-term memory store initialized")
        except Exception as e:
            print(f"⚠️ Warning: Could not setup store indexes: {e}")
            
        tools = get_all_tools()
        #memory_middleware = MemoryExtractionMiddleware()
        
        self.agent = create_agent(
            model=self.llm,
            tools=tools,
            checkpointer=self.checkpointer,
            store=long_term_store,
            system_prompt=Roami_travel_planner_system_prompt,
            #middleware=[memory_middleware],
            context_schema=Context
        )
        
        self._initialized = True
    
    async def _clear_checkpoint_completely(self, session_id: str) -> bool:
        try:
            database = self.client.get_database(settings.DATABASE_NAME)

            checkpoint_count = database["checkpoints"].count_documents(
                {"thread_id": session_id}
            )
            writes_count = database["checkpoint_writes"].count_documents(
                {"thread_id": session_id}
            )

            logger.warning(
                f"[checkpoint_clear] Clearing corrupted checkpoint "
                f"session={session_id} | "
                f"checkpoints={checkpoint_count} | writes={writes_count}"
            )

            if writes_count > 0:
                writes = list(
                    database["checkpoint_writes"]
                    .find(
                        {"thread_id": session_id},
                        {"_id": 0, "task_id": 1, "channel": 1, "type": 1},
                    )
                    .sort("_id", -1)
                    .limit(10)
                )
                logger.warning(f"[checkpoint_clear] Last writes: {writes}")

            database["checkpoints"].delete_many({"thread_id": session_id})
            database["checkpoint_writes"].delete_many({"thread_id": session_id})

            logger.info(f"[checkpoint_clear] Cleared session={session_id}")
            return True

        except Exception as e:
            logger.error(f"[checkpoint_clear] Failed: {e}", exc_info=True)
            return False
    
    async def get_response(
    self, session_id: str, user_input: str, user_mood: str, user_id: str
):
        await self._ensure_initialized()
        await self.session_manager.update_session_activity(session_id)

        await self.session_manager.save_travel_message(
            user_id=user_id,
            session_id=session_id,
            role="user",
            content=user_input,
        )

        config = {
            "configurable": {
                "thread_id": session_id,
                "user_id": user_id,
            }
        }

        formatted_input = (
            f"[SYSTEM CONTEXT — not visible to user]\n"
            f"today_date: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"user_id: {user_id}\n"
            f"mood: {user_mood}\n"
            f"[USER MESSAGE]\n"
            f"{user_input}"
        )

        context = Context(user_id=user_id)
        input_msg = {"messages": [HumanMessage(content=formatted_input)]}
        assistant_chunks: list[str] = []

        async def _stream_assistant(messages):
            current_run_id = None

            async for event in self.agent.astream_events(
                messages,
                config=config,
                context=context,
                version="v2",
            ):
                event_name = event.get("event")

                if event_name == "on_chain_start" and current_run_id is None:
                    current_run_id = event.get("run_id")

                if event_name != "on_chat_model_stream":
                    continue

                if event.get("run_id") != current_run_id:
                    parent_ids = event.get("parent_ids", [])
                    if current_run_id not in parent_ids:
                        continue

                chunk = event.get("data", {}).get("chunk")
                if not chunk:
                    continue

                if getattr(chunk, "tool_calls", None):
                    continue

                if hasattr(chunk, "content") and chunk.content:
                    assistant_chunks.append(chunk.content)
                    yield chunk.content

        for attempt in range(2):
            try:
                async for token in _stream_assistant(input_msg):
                    yield token

                # Save is isolated — its failure must never surface to the user
                final_text = "".join(assistant_chunks).strip()
                if final_text:
                    try:
                        await self.session_manager.save_travel_message(
                            user_id=user_id,
                            session_id=session_id,
                            role="assistant",
                            content=final_text,
                        )
                    except Exception as save_err:
                        logger.error(
                            f"[get_response] save_travel_message failed "
                            f"session={session_id} | {save_err}"
                        )
                return

            except Exception as e:
                error_msg = str(e)
                logger.error(
                    f"[get_response] Exception | attempt={attempt} | "
                    f"chunks_delivered={len(assistant_chunks)} | "
                    f"session={session_id} | error={error_msg}",
                    exc_info=True,
                )

                # Response already delivered — recover silently
                if assistant_chunks:
                    final_text = "".join(assistant_chunks).strip()
                    if final_text:
                        try:
                            await self.session_manager.save_travel_message(
                                user_id=user_id,
                                session_id=session_id,
                                role="assistant",
                                content=final_text,
                            )
                        except Exception as save_err:
                            logger.error(
                                f"[get_response] save after crash failed: {save_err}"
                            )
                    await self._clear_checkpoint_completely(session_id)
                    return  # User already has their answer — do not yield error

                # Nothing delivered — safe to retry or surface error
                if (
                    attempt == 0
                    and "tool_call_id" in error_msg
                    and "did not have response messages" in error_msg
                ):
                    await self._clear_checkpoint_completely(session_id)
                    continue

                yield "\n\n⚠️ Something went wrong. Please try again."
                return

    async def invoke(self, session_id: str, user_input: str, user_id: str) -> dict:
        """Get complete response (non-streaming)"""
        await self._ensure_initialized()
        await self.session_manager.update_session_activity(session_id)
        
        config = {
            "configurable": {
                "thread_id": session_id,
                "user_id": user_id
            }
        }
        
        context = Context(user_id=user_id)
        input_msg = {"messages": [HumanMessage(content=user_input)]}
        
        try:
            return await self.agent.ainvoke(input_msg, config=config, context=context)
        
        except Exception as e:
            error_msg = str(e)
            
            if "tool_call_id" in error_msg:
                # Try to clear and retry
                await self._clear_checkpoint_completely(session_id)
                try:
                    return await self.agent.ainvoke(input_msg, config=config, context=context)
                except Exception as retry_error:
                    return {"error": f"Session recovered but error occurred: {str(retry_error)}"}
            
            return {"error": str(e)}
    
    async def get_travel_chat_history(self, session_id: str, user_id: str):
        try:
            return await self.session_manager.get_travel_chat_history(
                session_id=session_id,
                user_id=user_id,
            )
        except Exception as e:
            raise e

    
    async def list_all_active_sessions(self, user_id: str,Type:str) -> list[dict]:
        """List all active sessions for a user"""
        await self._ensure_initialized()
        
        try:
            return await self.session_manager.get_session(user_id,Type)
        except Exception as e:
            print(f"Error listing sessions: {e}")
            return []
    
    async def create_session(self, user_id: str, session_id: str, first_message: str,Type:str) -> dict:
        """Create a new session with title generation"""
        await self._ensure_initialized()
        
        try:
            title = await self.generate_session_title(first_message)
            
            result = await self.session_manager.create_session(
                user_id=user_id,
                session_id=session_id,
                title=title,
                Type=Type

            )
            
            return result
        except Exception as e:
            print(f"Error creating session: {e}")
            return {"error": str(e)}
    
    async def clear_session(self, session_id: str, user_id: str) -> bool:
        await self._ensure_initialized()

        await self._clear_checkpoint_completely(session_id)

        result = await self.session_manager.delete_travel_session(
            session_id=session_id,
            user_id=user_id,
        )

        return result["session_deleted"] or result["messages_deleted"] > 0

    
    import json

    import json

    async def generate_session_title(self, first_message: str, ai_response: str = "") -> dict:
        """Generate session title from first message and AI response"""
        try:
            context = f"User: {first_message}"
            if ai_response:
                context += f"\nAssistant: {ai_response[:300]}"
            
            prompt = title_generation_prompt.format(first_message=context)
            
            # 🔍 ADD THIS: Log the full prompt
            logging.info(f"📝 FULL PROMPT:\n{prompt}")
            
            messages = [HumanMessage(content=prompt)]
            response = await self.title_llm.ainvoke(messages)
            
            # 🔍 ADD THIS: Log raw response
            logging.info(f"🤖 RAW LLM RESPONSE:\n{response.content}")
            
            response_text = response.content if hasattr(response, 'content') else str(response)
            
            # 🔍 ADD THIS: Log before cleanup
            logging.info(f"📋 BEFORE CLEANUP:\n{response_text}")
            
            # Better cleanup
            response_text = response_text.strip()
            
            # Remove markdown code blocks
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            elif response_text.startswith("```"):
                response_text = response_text[3:]
            
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            
            response_text = response_text.strip()
            
            # 🔍 ADD THIS: Log after cleanup
            logging.info(f"✨ AFTER CLEANUP:\n{response_text}")
            
            # Try to extract JSON if there's surrounding text
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group(0)
                logging.info(f"🎯 EXTRACTED JSON:\n{response_text}")
            
            response_dict = json.loads(response_text)
            
            # Validate
            if "title" not in response_dict or not response_dict["title"].strip():
                response_dict["title"] = first_message[:40] + "..." if len(first_message) > 40 else first_message
            
            if "subtitle" not in response_dict:
                response_dict["subtitle"] = ""
            
            response_dict["title"] = response_dict["title"][:60]
            response_dict["subtitle"] = response_dict["subtitle"][:100]
            
            logging.info(f"✅ FINAL RESULT: {response_dict}")
            return response_dict
            
        except json.JSONDecodeError as e:
            logging.error(f"❌ JSON DECODE ERROR: {e}")
            logging.error(f"📄 Failed to parse: {response_text if 'response_text' in locals() else 'N/A'}")
            return {
                "title": first_message[:40] + "..." if len(first_message) > 40 else first_message,
                "subtitle": ""
            }
        except Exception as e:
            logging.error(f"❌ GENERATION ERROR: {e}", exc_info=True)
            return {
                "title": first_message[:40] + "..." if len(first_message) > 40 else first_message,
                "subtitle": ""
            }
    async def get_user_memories(self, user_id: str, limit: int = 20) -> list[dict]:
        """Retrieve all stored memories for a user"""
        try:
            return await LongTermMemory_instance.list_memories(
                user_id=user_id,
                limit=limit
            )
        except Exception as e:
            print(f"Error retrieving memories: {e}")
            return []
    
    async def close(self):
        """Cleanup resources"""
        if self.client:
            self.client.close()
            self._initialized = False