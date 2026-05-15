import asyncio
from database import _db_load_history, _db_save_message

_session_cache: dict[str, list[dict]] = {}

pending_tool_calls:  dict[str, dict]           = {}
tool_call_events:    dict[str, asyncio.Event]  = {}
tool_call_decisions: dict[str, dict]           = {}

async def _get_session_history(session_id: str) -> list[dict]:
    if session_id not in _session_cache:
        _session_cache[session_id] = await _db_load_history(session_id)
    return _session_cache[session_id]

async def _append_to_history(session_id: str, msg: dict) -> None:
    history = await _get_session_history(session_id)
    history.append(msg)
    await _db_save_message(session_id, msg)
