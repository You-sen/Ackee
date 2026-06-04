"""
Long-term Memory with MongoDB - Fixed for Latest LangGraph Version
"""

import uuid
from langgraph.store.mongodb import MongoDBStore, create_vector_index_config
from langchain_openai import OpenAIEmbeddings
import os
from typing import Optional, Dict, Any, List
from ...config.config import settings

# Assuming you have a config module
# from ...config.config import settings

# os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY


class LongTermMemory:
    def __init__(self):
        """
        Initialize the Long Term Memory system.
        
        Args:
            mongodb_url: MongoDB connection string
            database_name: Name of the database
            openai_api_key: OpenAI API key for embeddings
        """
        self.mongodb_url = settings.DATABASE_URL
        self.database_name = settings.DATABASE_NAME
        os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
        self.store: Optional[MongoDBStore] = None
        self.index_config = None

    def create_index_config(self):
        """Create the vector index configuration."""
        if self.index_config is not None:
            return self.index_config
            
        embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        
        self.index_config = create_vector_index_config(
            embed=embeddings,
            dims=1536,
            fields=["content"],
            filters=["user_id", "type"]
        )
        
        return self.index_config

    def initialize_store(self) -> MongoDBStore:
        """
        Initialize the MongoDB store with embeddings using from_conn_string.
        
        Returns:
            MongoDBStore instance
        """
        if self.store is not None:
            return self.store

        index_config = self.create_index_config()

        # Use from_conn_string class method instead of __init__
        self.store = MongoDBStore.from_conn_string(
            conn_string=self.mongodb_url,
            db_name=self.database_name,
            collection_name="long_term_memories",
            index_config=index_config
        )
        
        return self.store

    async def setup_store(self):
        """
        Setup the store and create indexes.
        """
        store = self.initialize_store()
        try:
            await store.asetup()
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise e

    async def put_memory(
        self,
        content: str,
        user_id: str,
        namespace: tuple = ("memories",),
        key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        memory_type: str = "general",
    ) -> None:
        """
        Store memory with automatic duplicate check and vector embedding.
        
        Args:
            content: Text to remember
            user_id: User identifier
            namespace: Organization tuple
            key: Optional specific key
            metadata: Additional data
            memory_type: Category (preference, trip_plan, etc.)
        """
        store = self.initialize_store()
        
        # Check for duplicates first
        existing = await self.check_duplicate(content, user_id, namespace)
        if existing:
            print(f"Duplicate memory found, skipping: {content[:50]}...")
            return

        full_metadata = {
            "user_id": user_id,
            "type": memory_type,
            **(metadata or {})
        }

        # Store with embedding (happens automatically based on index config)
        await store.aput(
            namespace=(*namespace, user_id),
            key=key or f"mem_{uuid.uuid4().hex}",
            value={"content": content, **full_metadata}
        )

    async def check_duplicate(
        self,
        content: str,
        user_id: str,
        namespace: tuple = ("memories",),
        similarity_threshold: float = 0.95
    ) -> bool:
        """
        Check if very similar memory already exists.
        
        Args:
            content: Content to check
            user_id: User identifier
            namespace: Organization tuple
            similarity_threshold: Min score for duplicate (0-1)
            
        Returns:
            True if duplicate found
        """
        store = self.initialize_store()
        
        try:
            # Search for similar content
            results = await store.asearch(
                namespace_prefix=(*namespace, user_id),
                query=content,
                limit=1,
                filter={"user_id": user_id}
            )
            
            # Check if top result is too similar
            if results and len(results) > 0:
                top_result = results[0]
                if top_result.value.get("content", "") == content:
                    return True
                    
            return False
            
        except Exception as e:
            print(f"Duplicate check failed: {e}")
            return False

    async def get_memory(
        self,
        query: str,
        user_id: str,
        namespace: tuple = ("memories",),
        limit: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        memory_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieve relevant memories using semantic search.
        
        Args:
            query: Natural language query
            user_id: User filter
            namespace: Base namespace
            limit: Max results
            filters: Additional metadata filters
            memory_type: Optional type filter
            
        Returns:
            List of memories with content and metadata
        """
        store = self.initialize_store()

        full_filters = {
            "user_id": user_id,
            **(filters or {})
        }
        if memory_type:
            full_filters["type"] = memory_type

        # Semantic search happens automatically via vector index
        results = await store.asearch(
            namespace_prefix=(*namespace, user_id),
            query=query,
            limit=limit,
            filter=full_filters if full_filters else None
        )

        memories = []
        for result in results:
            memories.append({
                "content": result.value.get("content", ""),
                "user_id": result.value.get("user_id", ""),
                "type": result.value.get("type", ""),
                "metadata": {k: v for k, v in result.value.items() 
                           if k not in ["content", "user_id", "type"]},
                "key": result.key
            })

        return memories

    async def delete_memory(
        self,
        user_id: str,
        key: str,
        namespace: tuple = ("memories",)
    ) -> bool:
        """
        Delete a specific memory.
        
        Args:
            user_id: User identifier
            key: Memory key to delete
            namespace: Organization tuple
            
        Returns:
            True if deleted successfully
        """
        store = self.initialize_store()
        
        try:
            await store.adelete(
                namespace=(*namespace, user_id),
                key=key
            )
            return True
        except Exception as e:
            print(f"Delete failed: {e}")
            return False

    async def list_memories(
        self,
        user_id: str,
        namespace: tuple = ("memories",),
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        List all memories for a user.
        
        Args:
            user_id: User identifier
            namespace: Base namespace
            limit: Max number of memories to return
            
        Returns:
            List of memories
        """
        store = self.initialize_store()
        
        try:
            # Use alist to get all items in the namespace
            items = await store.alist(namespace_prefix=(*namespace, user_id))
            
            memories = []
            for item in items[:limit]:
                memories.append({
                    "content": item.value.get("content", ""),
                    "type": item.value.get("type", "general"),
                    "user_id": item.value.get("user_id", ""),
                    "metadata": {k: v for k, v in item.value.items() 
                            if k not in ["content", "type", "user_id"]},
                    "key": item.key
                })
            
            return memories
        except Exception as e:
            print(f"Error listing memories: {e}")
            return []

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleanup if needed."""
        pass


# Example usage:
async def example_usage():
    """Example of how to use the LongTermMemory class."""
    
    # Initialize with your credentials
    memory = LongTermMemory(
        mongodb_url="mongodb://localhost:27017",
        database_name="my_database",
        openai_api_key="your-openai-key"
    )
    
    # Setup the store (run once)
    await memory.setup_store()
    
    # Store a memory
    await memory.put_memory(
        content="User loves Italian food and prefers vegetarian options",
        user_id="user123",
        memory_type="preference"
    )
    
    # Retrieve memories
    results = await memory.get_memory(
        query="food preferences",
        user_id="user123",
        limit=5
    )
    
    for result in results:
        print(f"Memory: {result['content']}")
    
    # List all memories
    all_memories = await memory.list_memories(user_id="user123")
    
    # Delete a memory
    if all_memories:
        await memory.delete_memory(
            user_id="user123",
            key=all_memories[0]["key"]
        )