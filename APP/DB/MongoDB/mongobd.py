from langchain_mongodb.chat_message_histories import MongoDBChatMessageHistory 
from ...config.config import settings
from fastapi import HTTPException
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import json

class MongoDBSessionManager:
    """Manages session metadata and chat history"""
    
    def __init__(self):
        self.client = AsyncIOMotorClient(settings.DATABASE_URL)
        self.db = self.client[settings.DATABASE_NAME]
        self.session_collection = self.db[settings.COLLECTION_SESSION]
        self.message_collection = self.db[settings.COLLECTION_REASSURES_NAME]
        self.travel_chat_collection = self.db[settings.COLLECTION_TRAVEL_NAME]
        self.user_collection = self.db[settings.COLLECTION_USER]

    async def get_user(self, id: str) -> str:
        """Get user by id. Returns a JSON string."""
        try:
            user = await self.user_collection.find_one(
                {"_id": ObjectId(id)},
                {"_id": 0, "name": 1, "tripExperience": 1, "address": 1,
                 "placeId": 1, "soloTravelConfidence": 1}
            )
            if user is None:
                return json.dumps({"status": "not_found", "message": f"No user found with id={id}"})
            # default=str safely handles datetime, ObjectId, etc.
            return json.dumps(user, default=str)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    def get_session_message(self, session_id: str) -> MongoDBChatMessageHistory:
        """Get message history for a session"""
        return MongoDBChatMessageHistory(
            connection_string=settings.DATABASE_URL,
            session_id=session_id,
            database_name=settings.DATABASE_NAME,
            collection_name=settings.COLLECTION_REASSURES_NAME,
        )
    async def save_travel_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        try:
            await self.travel_chat_collection.insert_one({
                "session_id": session_id,
                "user_id": user_id,
                "role": role,
                "content": content,
                "created_at": datetime.now(timezone.utc),
            })
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    
    async def get_travel_chat_history(
        self,
        session_id: str,
        user_id: str,
        limit: int = 100,
    ) -> list[dict]:
        try:
            cursor = self.travel_chat_collection.find(
                    {
                        "$or": [
                            {"session_id": session_id, "user_id": user_id},
                            {"SessionId": session_id, "UserId": user_id}
                        ]
                    },
                    {"_id": 0}
                ).sort("created_at", 1)
            

            return await cursor.to_list(length=limit)

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def save_reassures_message(
        self,
        session_id: str,
        role: str,
        content: str,
        user_id: str = None,
    ) -> None:
        try:
            doc = {
                "SessionId": session_id,
                "role": role,
                "content": content,
                "created_at": datetime.now(timezone.utc),
            }
            if user_id:
                doc["UserId"] = user_id
            await self.message_collection.insert_one(doc)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_reassures_chat_history(
        self,
        session_id: str,
        user_id: str = None,
        limit: int = 100,
    ) -> list[dict]:
        try:
            query = {"$or": [{"session_id": session_id}, {"SessionId": session_id}]}
            if user_id:
                query = {
                    "$or": [
                        {"session_id": session_id, "user_id": user_id},
                        {"SessionId": session_id, "UserId": user_id}
                    ]
                }
            
            cursor = self.message_collection.find(
                    query,
                    {"_id": 0}
                ).sort("created_at", 1)
            

            return await cursor.to_list(length=limit)

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    
    async def delete_travel_session(
        self,
        session_id: str,
        user_id: str,
    ) -> dict:
        try:
            messages_result = await self.travel_chat_collection.delete_many({
                "session_id": session_id,
                "user_id": user_id,
            })

            session_result = await self.session_collection.delete_one({
                "session_id": session_id,
                "user_id": user_id,
            })

            return {
                "session_deleted": session_result.deleted_count > 0,
                "messages_deleted": messages_result.deleted_count,
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
            
    async def get_session(self, user_id: str,Type:str) -> list[dict]:
        """Get all sessions for a user"""
        try:
            query = {"user_id": user_id, "type": Type}
            print(f"Executing query: {query}")

            cursor = self.session_collection.find(
                query, 
                {"session_id": 1, "title": 1,'subtitle':1, "user_id": 1, "created_at": 1, "_id": 0}
            ).sort("created_at", -1) 
            
            sessions = await cursor.to_list(length=100)
            
            # Debug: Check if we got results
            if not sessions:
                print("No sessions found for the given criteria.")
                
            return sessions

        except Exception as e:
            # Log the full error for your own debugging
            print(f"Detailed Error: {e}")
            raise HTTPException(
                status_code=500, 
                detail="Internal Server Error" # Keep detail vague for users, log details locally
            )
    
    async def create_session(
        self, 
        user_id: str, 
        session_id: str, 
        title: str,
        subtitle:str,
        Type:str
    ) -> dict:
        """Create a new session"""
        try:
            result = await self.session_collection.insert_one({
                "session_id": session_id,
                "user_id": user_id,
                "title": title,
                "subtitle":subtitle,
                "type":Type,
                "created_at": datetime.now(timezone.utc),
                "last_activity": datetime.now(timezone.utc)
            })
            
            return {
                "session_id": session_id,
                "user_id": user_id,
                "title": title,
                "subtitle":subtitle,
                "created": True
            }

        except Exception as e:
            # Handle duplicate key error
            if "11000" in str(e) or "duplicate" in str(e).lower():
                return {
                    "session_id": session_id,
                    "user_id": user_id,
                    "title": title,
                    "created": False,
                    "message": "Session already exists"
                }
            raise HTTPException(
                status_code=500, 
                detail=f"Error creating session: {str(e)}"
            )
    
    async def update_session_activity(self, session_id: str) -> None:
        """Update last activity timestamp for a session"""
        try:
            await self.session_collection.update_one(
                {"session_id": session_id},
                {"$set": {"last_activity": datetime.now(timezone.utc)}}
            )
        except Exception as e:
            print(f"Error updating session activity: {e}")
    
    async def delete_session(self, session_id: str, user_id: str) -> dict:
        """Delete a session and its messages"""
        try:
            # Delete session metadata
            session_result = await self.session_collection.delete_one({
                "session_id": session_id,
                "user_id": user_id
            })
            
            # Delete session messages
            message_result = await self.message_collection.delete_many({
                "SessionId": session_id
            })
            
            return {
                "session_deleted": session_result.deleted_count > 0,
                "messages_deleted": message_result.deleted_count
            }
        
        except Exception as e:
            raise HTTPException(
                status_code=500, 
                detail=f"Error deleting session: {str(e)}"
            )
    
    async def close(self):
        """Close MongoDB connection"""
        self.client.close()


    async def get_ressure_AI_message(self, session_id: str, user_id: str) -> dict:
        """Get reassurence AI message for a session"""
        try:
            message = await self.message_collection.find_one({
                "SessionId": session_id,
                "UserId": user_id,
                "role": "assistant"
            },{"content":1})
            return message
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    async def get_travel_AI_message(self, session_id: str, user_id: str) -> dict:
        """Get travel AI message for a session"""
        try:
            message = await self.travel_chat_collection.find_one({
                "SessionId": session_id,
                "UserId": user_id,
                "role": "assistant"
            },{"content":1})
            return message
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))