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

    # Swarm Mode parameter
    swarm_mode:    Optional[str] = "base" # base, pro, premium

    # Extended Image Generation Parameters
    generation_mode: Optional[str] = "cloud" # local or cloud
    aspect_ratio: Optional[str] = "1:1" # 1:1, 16:9, 9:16, 4:3
    steps: Optional[int] = 20 # 10 to 50
    negative_prompt: Optional[str] = ""
    cfg_scale: Optional[float] = 7.0

class ToolDecisionRequest(BaseModel):
    call_id:  str
    approved: bool

class ClearHistoryRequest(BaseModel):
    session_id: str
