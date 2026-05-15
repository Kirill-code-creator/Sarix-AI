from fastapi import APIRouter
from models import ClearHistoryRequest
from services.state import _session_cache
from database import _db_clear_history, _db_load_history, _db_get_all_sessions

router = APIRouter()

@router.post("/clear-history")
async def clear_history(req: ClearHistoryRequest):
    _session_cache.pop(req.session_id, None)
    n = await _db_clear_history(req.session_id)
    return {"ok": True, "cleared": n}

@router.get("/history/{session_id}")
async def get_history(session_id: str):
    history = await _db_load_history(session_id)
    visible = [
        msg for msg in history
        if msg["role"] in ("user", "assistant") and msg.get("content")
    ]
    return {"session_id": session_id, "messages": visible}

@router.get("/sessions")
async def get_sessions():
    sessions = await _db_get_all_sessions()
    return {"sessions": sessions}
