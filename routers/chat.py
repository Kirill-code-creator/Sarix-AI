import json
import base64
import io
import docx
from pypdf import PdfReader
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from models import ChatRequest, ToolDecisionRequest
from services.agent import _route_model, _agent_loop
from services.state import _append_to_history, tool_call_events, tool_call_decisions
from logger import logger

router = APIRouter()

@router.post("/chat")
async def chat(req: ChatRequest):
    message_content = req.message

    if req.files:
        files_text = "\n\nПрикрепленные файлы:\n"
        image_contents = []
        for f in req.files:
            fname = f.get("name", "unknown")
            ftype = f.get("type", "")
            data_b64 = f.get("data", "")

            try:
                # Remove data URL prefix if present
                if "," in data_b64:
                    data_b64 = data_b64.split(",", 1)[1]
                raw_data = base64.b64decode(data_b64)

                if ftype == "application/pdf":
                    reader = PdfReader(io.BytesIO(raw_data))
                    text = "".join(page.extract_text() or "" for page in reader.pages)
                    files_text += f"--- {fname} (PDF) ---\n{text}\n"
                elif ftype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                    doc = docx.Document(io.BytesIO(raw_data))
                    text = "\n".join(p.text for p in doc.paragraphs)
                    files_text += f"--- {fname} (DOCX) ---\n{text}\n"
                elif ftype == "text/plain":
                    text = raw_data.decode("utf-8")
                    files_text += f"--- {fname} (TXT) ---\n{text}\n"
                elif ftype.startswith("image/"):
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{ftype};base64,{data_b64}"
                        }
                    })
                elif ftype.startswith("video/"):
                    # Only some models support video natively, passing base64
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{ftype};base64,{data_b64}"
                        }
                    })
            except Exception as e:
                logger.warning(f"Ошибка парсинга файла {fname}: {e}")

        if len(files_text) > 25:
            message_content += files_text

        if image_contents:
            # Multi-modal content
            message_content = [{"type": "text", "text": message_content}] + image_contents

    await _append_to_history(req.session_id, {"role": "user", "content": message_content})
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
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            top_k=req.top_k,
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
