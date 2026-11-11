"""
chat_api.py
POST /chat            -> grounded natural-language answer built from the real database
GET  /chat/questions  -> the 6 mandatory demo questions (for UI buttons)
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

import chat_engine


class ChatIn(BaseModel):
    message: str = Field(..., min_length=2, max_length=500, examples=["إيه حالة الكاميرات؟"])
    hours: Optional[int] = Field(None, ge=1, le=168, description="Look-back window in hours ending at the latest event. Default: taken from the question ('last 3 days') or 72")
    use_llm: bool = Field(True, description="false = deterministic answer only (no Ollama call)")


def create_chat_router(db):
    router = APIRouter(prefix="/chat", tags=["Chat"])

    @router.post("", summary="Ask the building a question in natural language")
    def chat(body: ChatIn):
        """Classifies the question, collects facts from the database, lets the local model phrase the answer,
        validates it against the facts, and falls back to a deterministic answer if validation fails."""
        message = body.message.strip()
        lang = chat_engine.detect_lang(message)
        hours = body.hours or chat_engine.parse_hours(message)
        intent = chat_engine.classify(message, body.use_llm)      # outside the DB block: may call Ollama
        try:
            with db() as cur:                                     # connection is released before the LLM call
                ctx = chat_engine.gather(cur, message, intent, hours, lang)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        return jsonable_encoder(chat_engine.compose(ctx, body.use_llm))

    @router.get("/questions", summary="The six mandatory questions")
    def questions():
        return {"questions": chat_engine.SUGGESTED_QUESTIONS, "extra": chat_engine.EXTRA_QUESTIONS,
                "intents": chat_engine.INTENT_NAMES}

    return router