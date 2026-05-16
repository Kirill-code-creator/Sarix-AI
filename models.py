from pydantic import BaseModel

from typing import Optional, List, Dict, Any

class ChatRequest(BaseModel):
    session_id:    str
    message:       str
    api_key:       str
    # model теперь не используется напрямую — выбирается оркестратором.
    # Поле оставлено для обратной совместимости.
    model:         str = "auto"
    system_prompt: str = (
        "Ты — мощный AI-ассистент NeuralChat. "
        "Ты можешь выполнять команды в терминале и управлять компьютером. "
        "Отвечай на языке пользователя. Будь точен и лаконичен."
    )
    max_tokens:    Optional[int]   = 4096
    temperature:   Optional[float] = 0.7
    top_p:         Optional[float] = 1.0
    top_k:         Optional[int]   = 0
    files:         Optional[List[Dict[str, Any]]] = None

class ToolDecisionRequest(BaseModel):
    call_id:  str
    approved: bool

class ClearHistoryRequest(BaseModel):
    session_id: str
