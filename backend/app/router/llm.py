from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
import httpx
import json
import re

from app.config.config import settings

llm_router = APIRouter(prefix="/llm", tags=["llm"])

SYSTEM_PROMPT = """You are a Steam game recommender. The user will describe what kind of game they want.

Respond ONLY with a valid JSON object in this exact format — no explanation, no markdown, no extra text:
{
  "games": ["Game Name 1", "Game Name 2", "Game Name 3"]
}

Rules:
- Only recommend games available on Steam.
- Use the exact Steam store name of each game.
- Do not recommend any game the user explicitly mentions by name in their request.
- Return between 3 and 10 games.
- Do not include any text outside the JSON object."""


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[Message]


async def _request_completion(client, base_url, api_key, model, messages):
    res = await client.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages},
        timeout=60.0,
    )
    res.raise_for_status()
    return res


@llm_router.post("/chat/completions")
async def chat_completions(request: ChatRequest):
    messages_with_system = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *[m.model_dump() for m in request.messages],
    ]

    async with httpx.AsyncClient() as client:
        try:
            res = await _request_completion(
                client, settings.LLM_BASE_URL, settings.LLM_API_KEY,
                settings.LLM_MODEL, messages_with_system,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and settings.LLM_FALLBACK_API_KEY:
                try:
                    res = await _request_completion(
                        client, settings.LLM_FALLBACK_BASE_URL, settings.LLM_FALLBACK_API_KEY,
                        settings.LLM_FALLBACK_MODEL, messages_with_system,
                    )
                except httpx.HTTPStatusError as e2:
                    raise HTTPException(status_code=e2.response.status_code, detail=str(e2))
                except httpx.RequestError as e2:
                    raise HTTPException(status_code=503, detail=str(e2))
            else:
                raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=str(e))

    raw = res.json()
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")

    try:
        parsed = json.loads(content)
        game_names = parsed.get("games", [])
    except (json.JSONDecodeError, AttributeError):
        raise HTTPException(status_code=502, detail=f"LLM returned unexpected format: {content}")

    from app.main import apps

    results = []
    for name in game_names:
        escaped = re.escape(name)
        cursor = apps.find({"name": {"$regex": f"^{escaped}$", "$options": "i"}}).limit(1)
        docs = await cursor.to_list(length=1)
        if not docs:
            cursor = apps.find({"name": {"$regex": f"^{escaped}", "$options": "i"}}).limit(1)
            docs = await cursor.to_list(length=1)
        results.extend(docs)

    return {
        "games": [
            {
                "appid": int(doc["_id"]),
                "name": doc["name"],
                "tags": doc.get("tags", []),
                "header_image": doc.get("header_image", ""),
            }
            for doc in results
        ]
    }
