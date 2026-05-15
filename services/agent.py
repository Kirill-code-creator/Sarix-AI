import asyncio
import json
import re
from typing import Any
import httpx

from logger import logger
from config import (
    OPENROUTER_BASE_URL, CONTEXT_WINDOW_SIZE, SERVER_HOST, SERVER_PORT,
    ORCHESTRATOR_MODEL, ROUTING_MAP, ROUTING_FALLBACK, ORCHESTRATOR_SYSTEM
)
from services.state import _get_session_history, _append_to_history, pending_tool_calls, tool_call_events, tool_call_decisions
from services.computer import _run_computer_control
from services.tools import _classify_command, _run_shell_stream

# Здесь мы выносим TOOLS как константу для agent.py
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "execute_terminal_command",
            "description": (
                "Выполняет команду в терминале ОС Windows/Linux/macOS. "
                "Некоторые безопасные команды (pip install, dir, echo и т.п.) "
                "выполняются автоматически. Опасные команды (rm, del, format) "
                "требуют явного подтверждения пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Команда для выполнения в терминале."
                    },
                    "description": {
                        "type": "string",
                        "description": "Краткое описание: что делает эта команда и зачем."
                    },
                },
                "required": ["command", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_computer",
            "description": (
                "Управляет мышью и клавиатурой компьютера с помощью pyautogui. "
                "Позволяет автоматизировать работу с офисными программами и UI. "
                "Используй для: перемещения курсора, кликов, ввода текста, "
                "нажатия горячих клавиш."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["move", "click", "double_click", "right_click",
                                 "type_text", "press_key", "hotkey", "screenshot"],
                        "description": (
                            "Тип действия:\n  move, click, type_text, etc"
                        ),
                    },
                    "x": {
                        "type": "integer",
                        "description": "Координата X"
                    },
                    "y": {
                        "type": "integer",
                        "description": "Координата Y"
                    },
                    "text": {
                        "type": "string",
                        "description": "Текст"
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Клавиши для hotkey"
                    },
                    "duration": {
                        "type": "number",
                        "description": "Время движения мыши в секундах"
                    },
                },
                "required": ["action"],
            },
        },
    },
]

async def _call_openrouter(
    messages: list[dict],
    api_key:  str,
    model:    str,
    tools:    list[dict] | None = None,
) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  f"http://{SERVER_HOST}:{SERVER_PORT}",
        "X-Title":       "NeuralChat Desktop",
    }
    payload: dict[str, Any] = {
        "model":       model,
        "messages":    messages,
        "temperature": 0.7,
        "max_tokens":  4096,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )
        r.raise_for_status()
        return r.json()

async def _route_model(message: str, api_key: str) -> str:
    try:
        orchestrator_messages = [
            {"role": "system", "content": ORCHESTRATOR_SYSTEM},
            {"role": "user",   "content": message[:500]},
        ]
        logger.info(f"[Оркестратор] Анализирую запрос: {message[:80]!r}...")
        resp = await _call_openrouter(
            messages=orchestrator_messages,
            api_key=api_key,
            model=ORCHESTRATOR_MODEL,
            tools=None,
        )
        raw = resp["choices"][0]["message"]["content"].strip()
        logger.info(f"[Оркестратор] Ответ: {raw!r}")
        raw_clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        parsed = json.loads(raw_clean)
        category = parsed.get("category", "chat").lower()
        selected_model = ROUTING_MAP.get(category, ROUTING_FALLBACK)
        logger.info(f"[Оркестратор] Категория: {category!r} → Модель: {selected_model}")
        return selected_model
    except Exception as exc:
        logger.warning(f"[Оркестратор] Ошибка роутинга ({exc}), используем fallback: {ROUTING_FALLBACK}")
        return ROUTING_FALLBACK

async def _agent_loop(
    session_id:    str,
    api_key:       str,
    model:         str,
    system_prompt: str,
):
    def sse(t: str, payload: dict) -> str:
        return "data: " + json.dumps({"type": t, **payload}, ensure_ascii=False) + "\n\n"

    for iteration in range(10):
        history = await _get_session_history(session_id)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-CONTEXT_WINDOW_SIZE:])

        try:
            yield sse("thinking", {"message": "Обрабатываю..."})
            resp = await _call_openrouter(messages, api_key, model, TOOLS)
        except Exception as exc:
            yield sse("error", {"message": str(exc)})
            return

        choice = resp["choices"][0]
        finish = choice.get("finish_reason", "")
        msg    = choice["message"]

        await _append_to_history(session_id, msg)

        if finish == "stop" or (finish != "tool_calls" and msg.get("content")):
            if content := msg.get("content", ""):
                yield sse("assistant_message", {"content": content})
            if usage := resp.get("usage", {}):
                yield sse("usage", {
                    "prompt_tokens":     usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens":      usage.get("total_tokens", 0),
                    "model":             model,
                })
            yield sse("done", {})
            return

        if finish == "tool_calls" or msg.get("tool_calls"):
            for tc in msg.get("tool_calls", []):
                call_id = tc["id"]
                fn_name = tc["function"]["name"]

                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name == "execute_terminal_command":
                    command     = fn_args.get("command", "")
                    description = fn_args.get("description", "")
                    cmd_class   = _classify_command(command)

                    logger.info(f"[{session_id}] Команда: {command!r}, класс: {cmd_class}")

                    approved = False
                    if cmd_class == "safe":
                        yield sse("tool_auto_approved", {
                            "call_id":     call_id,
                            "command":     command,
                            "description": description,
                        })
                        approved = True
                    else:
                        ev = asyncio.Event()
                        tool_call_events[call_id]   = ev
                        pending_tool_calls[call_id] = {
                            "command":     command,
                            "description": description,
                            "dangerous":   cmd_class == "dangerous",
                        }

                        yield sse("tool_request", {
                            "call_id":     call_id,
                            "tool_name":   fn_name,
                            "command":     command,
                            "description": description,
                            "dangerous":   cmd_class == "dangerous",
                        })

                        try:
                            await asyncio.wait_for(ev.wait(), timeout=300.0)
                        except asyncio.TimeoutError:
                            tool_result = json.dumps({"error": "Таймаут подтверждения."})
                            yield sse("tool_timeout", {"call_id": call_id})
                        else:
                            decision = tool_call_decisions.pop(call_id, {})
                            approved = decision.get("approved", False)
                            if not approved:
                                tool_result = json.dumps({"error": "Пользователь отклонил команду."})
                                yield sse("tool_rejected", {"call_id": call_id})

                        tool_call_events.pop(call_id, None)
                        pending_tool_calls.pop(call_id, None)

                    if approved:
                        yield sse("tool_executing", {
                            "call_id": call_id,
                            "command": command,
                        })

                        # Streaming shell execution
                        full_output = ""
                        async for line in _run_shell_stream(command):
                            full_output += line
                            yield sse("tool_output", {
                                "call_id": call_id,
                                "output": line
                            })

                        result = {
                            "success": True, # Simplification since returncode not captured in async generator yet
                            "stdout": full_output.strip() or "(нет вывода)",
                            "command": command,
                        }
                        tool_result = json.dumps(result, ensure_ascii=False)
                        yield sse("tool_result", {
                            "call_id": call_id,
                            "result":  result,
                        })

                elif fn_name == "control_computer":
                    action = fn_args.get("action", "")
                    if action == "screenshot":
                        yield sse("tool_auto_approved", {
                            "call_id":   call_id,
                            "command":   f"[control_computer] action=screenshot",
                            "description": "Сделать скриншот экрана",
                        })
                        result = await _run_computer_control(action, **fn_args)
                        tool_result = json.dumps(result, ensure_ascii=False)
                        yield sse("tool_result", {"call_id": call_id, "result": result})
                    else:
                        from services.computer import _describe_computer_action
                        human_readable = _describe_computer_action(action, fn_args)

                        ev = asyncio.Event()
                        tool_call_events[call_id]   = ev
                        pending_tool_calls[call_id] = {
                            "command":     human_readable,
                            "description": f"Управление ПК: {action}",
                        }

                        yield sse("tool_request", {
                            "call_id":     call_id,
                            "tool_name":   fn_name,
                            "command":     human_readable,
                            "description": f"Управление ПК через pyautogui: {action}",
                            "dangerous":   False,
                        })

                        try:
                            await asyncio.wait_for(ev.wait(), timeout=300.0)
                        except asyncio.TimeoutError:
                            tool_result = json.dumps({"error": "Таймаут подтверждения."})
                            yield sse("tool_timeout", {"call_id": call_id})
                        else:
                            decision = tool_call_decisions.pop(call_id, {})
                            if decision.get("approved"):
                                yield sse("tool_executing", {
                                    "call_id": call_id,
                                    "command": human_readable,
                                })
                                result = await _run_computer_control(action, **fn_args)
                                tool_result = json.dumps(result, ensure_ascii=False)
                                yield sse("tool_result", {
                                    "call_id": call_id,
                                    "result":  result,
                                })
                            else:
                                tool_result = json.dumps({"error": "Пользователь отклонил действие."})
                                yield sse("tool_rejected", {"call_id": call_id})

                        tool_call_events.pop(call_id, None)
                        pending_tool_calls.pop(call_id, None)
                else:
                    tool_result = json.dumps({
                        "error": f"Инструмент '{fn_name}' не найден в этой версии."
                    })
                    logger.warning(f"[{session_id}] Неизвестный инструмент: {fn_name!r}")

                await _append_to_history(session_id, {
                    "role":         "tool",
                    "tool_call_id": call_id,
                    "content":      tool_result,
                })

            continue

        yield sse("done", {})
        return

    yield sse("error", {"message": "Агент превысил лимит итераций (10)."})
