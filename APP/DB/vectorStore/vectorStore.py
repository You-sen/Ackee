from langchain_core.documents import Document
from langchain_mongodb import MongoDBAtlasVectorSearch
from langchain_openai import OpenAIEmbeddings
from pymongo import MongoClient
from ..config.config import settings
import os

os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY
class vectorStore:
    def __init__(self):
        self.mongodb_url = settings.smongodb_url
        self.database_name = settings.database_name
        self.collection_name = settings.Vector_collection_name
        self.vector_index_name = settings.vector_index_name
        self.embedding = OpenAIEmbeddings()

    def create_vector_store(self):
        client = MongoClient(self.mongodb_url)

        DB_NAME = self.database_name
        COLLECTION_NAME = self.collection_name
        ATLAS_VECTOR_SEARCH_INDEX_NAME = self.vector_index_name

        MONGODB_COLLECTION = client[DB_NAME][COLLECTION_NAME]

        vector_store = MongoDBAtlasVectorSearch(
            collection=MONGODB_COLLECTION,
            embedding=self.embedding,
            index_name=ATLAS_VECTOR_SEARCH_INDEX_NAME,
            relevance_score_fn="cosine",
        )

        vector_store.create_vector_search_index(dimensions=1536)
        return vector_store
    
    def add_document(self, text: str, metadata: dict):
        vector_store = self.create_vector_store()
        document = Document(page_content=text, metadata=metadata)
        vector_store.add_texts(texts=[document], metadatas=[metadata])
        return {"status": "success", "message": "Document added to vector store"}
    
    def search_documents(self, query: str, n_results: int = 5, user_id: str = None, session_id: str = None) -> list:
        vector_store = self.create_vector_store()
        docs = vector_store.similarity_search(query, k=n_results , filter={"user_id": user_id , "session_id": session_id} if user_id and session_id else None)
        if not docs:
            return "No relevant memories found."
        
        return "\n".join([d.page_content for d in docs])