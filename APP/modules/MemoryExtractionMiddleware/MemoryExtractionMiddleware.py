"""
Memory Extraction Middleware - Fixed for LangChain v1
"""

from typing import Any, Optional
from langchain.agents.middleware import AgentMiddleware
from langchain.agents import AgentState  
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.runtime import Runtime  
from openai import OpenAI  
import os
import json
import uuid
import asyncio

from ...prompt.prompt import Extract_memory_system_prompt


class MemoryExtractionMiddleware(AgentMiddleware):
    """
    Automatically extracts and persists long-term memories
    every N messages. Uses SYNC hooks as required by LangChain v1.
    """

    def __init__(self, extract_every: int = 20):
        super().__init__()
        self.extract_every = extract_every
        # ✅ Use SYNC OpenAI client for the initial call
        self.llm_sync = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def after_model(
        self,
        state: AgentState,  
        runtime: Runtime, 
    ) -> dict[str, Any] | None:
        """
        Extract memories after model runs, every N messages.
        This is a SYNC hook as required by LangChain v1.
        
        Args:
            state: Current agent state containing messages
            runtime: Runtime context with store and config
            
        Returns:
            State updates or None
        """
        messages = state.get("messages", [])
        
        # Messages should already be resolved in sync hooks
        if not isinstance(messages, list):
            print(f"Warning: messages is not a list, got {type(messages)}")
            return None
        
        if len(messages) < self.extract_every:
            return None

        last_count = state.get("last_memory_extraction_count", 0)
        current_count = len(messages)

        if current_count < last_count + self.extract_every:
            return None

        user_id = self._resolve_user_id(runtime)
        if not user_id:
            print("Warning: No user_id found in context, skipping memory extraction")
            return None

        # ✅ Schedule async work without blocking
        # We use asyncio.create_task to run the async store operations
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule as background task
                asyncio.create_task(
                    self._extract_and_store_async(
                        messages=messages[-self.extract_every:],
                        runtime=runtime,
                        user_id=user_id,
                    )
                )
            else:
                # If no loop is running, create one
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(
                    self._extract_and_store_async(
                        messages=messages[-self.extract_every:],
                        runtime=runtime,
                        user_id=user_id,
                    )
                )
        except Exception as e:
            print(f"Error scheduling memory extraction: {e}")

        return {
            "last_memory_extraction_count": current_count
        }

    async def _extract_and_store_async(
        self,
        messages: list[Any],
        runtime: Runtime,
        user_id: str,
    ) -> None:
        """
        Extract memories from conversation and store them (ASYNC).
        This runs in the background after the sync hook returns.
        
        Args:
            messages: Recent conversation messages
            runtime: Runtime context with store
            user_id: User identifier
        """
        store = runtime.store
        if not store:
            print("Warning: No store available for memory extraction")
            return

        conversation_text = self._serialize_messages(messages)
        if not conversation_text:
            return

        try:
            # ✅ Use sync client for LLM call in background task
            response = self.llm_sync.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.2,
                max_tokens=500,
                messages=[
                    {
                        "role": "system",
                        "content": Extract_memory_system_prompt,
                    },
                    {
                        "role": "user",
                        "content": (
                            "Extract long-term user memories as JSON.\n\n"
                            f"Conversation:\n{conversation_text}"
                        ),
                    },
                ],
            )

            raw_output = response.choices[0].message.content.strip()

            try:
                memories = json.loads(raw_output)
            except json.JSONDecodeError as e:
                print(f"Failed to parse memory extraction JSON: {e}")
                return

            if not isinstance(memories, list):
                print(f"Expected list of memories, got: {type(memories)}")
                return

            namespace = ("memories", user_id)

            # Store each extracted memory
            for memory in memories:
                content = memory.get("content", "").strip()
                memory_type = memory.get("type", "general")

                if not content:
                    continue

                # Check for duplicates
                try:
                    existing = await store.asearch(
                        namespace=namespace,
                        query=content,
                        limit=1,
                    )

                    if existing and existing[0].value.get("content") == content:
                        print(f"Skipping duplicate memory: {content[:50]}...")
                        continue
                except Exception as e:
                    print(f"Error checking for duplicates: {e}")
                    # Continue anyway to avoid losing memory

                # Store the memory
                try:
                    await store.aput(
                        namespace=namespace,
                        key=f"mem_{uuid.uuid4().hex}",
                        value={
                            "content": content,
                            "type": memory_type,
                            "user_id": user_id,
                            "source": "auto_extracted",
                        },
                    )
                    print(f"✅ Stored memory: {content[:50]}...")
                except Exception as e:
                    print(f"❌ Error storing memory: {e}")

        except Exception as e:
            print(f"❌ Error in memory extraction: {e}")

    def _serialize_messages(self, messages: list[Any]) -> str:
        """
        Convert messages to readable text format.
        
        Args:
            messages: List of message objects
            
        Returns:
            Formatted conversation string
        """
        lines = []

        if not isinstance(messages, list):
            return ""

        for msg in messages:
            if isinstance(msg, HumanMessage):
                lines.append(f"User: {msg.content}")
            elif isinstance(msg, AIMessage):
                # Only include text content, not tool calls
                if msg.content:
                    lines.append(f"Assistant: {msg.content}")

        return "\n".join(lines)

    def _resolve_user_id(self, runtime: Runtime) -> str | None:
        """
        Get user_id from runtime context.
        
        Args:
            runtime: Runtime context
            
        Returns:
            User ID string or None
        """
        # ✅ Try config first (most reliable)
        user_id = runtime.config.get("configurable", {}).get("user_id")
        if user_id:
            return user_id
        
        # ✅ Fallback to context (TypedDict access)
        if runtime.context and isinstance(runtime.context, dict):
            return runtime.context.get("user_id")
        
        return None