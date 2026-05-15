import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from models import ChatRequest, ToolDecisionRequest
from services.agent import _route_model, _agent_loop
from services.state import _append_to_history, tool_call_events, tool_call_decisions

router = APIRouter()

@router.post("/chat")
async def chat(req: ChatRequest):
    await _append_to_history(req.session_id, {"role": "user", "content": req.message})
    selected_model = await _route_model(req.message, req.api_key)

    async def stream_with_model_event():
        yield "data: " + json.dumps({
            "type":  "model_selected",
            "model": selected_model,
        }, ensure_ascii=False) + "\n\n"

        async for event in _agent_loop(
            session_id=req.session_id,
            api_key=req.api_key,
            model=selected_model,
            system_prompt=req.system_prompt,
        ):
            yield event

    return StreamingResponse(
        stream_with_model_event(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/tool-decision")
async def tool_decision(req: ToolDecisionRequest):
    if req.call_id not in tool_call_events:
        raise HTTPException(404, f"call_id {req.call_id!r} не найден или уже обработан")
    tool_call_decisions[req.call_id] = {"approved": req.approved}
    tool_call_events[req.call_id].set()
    return {"ok": True}
