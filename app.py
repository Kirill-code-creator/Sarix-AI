# ==============================================================
# app.py — NeuralChat Desktop v5.0
#
# НОВОЕ в v5.0:
#   1. Умный роутинг моделей (Оркестратор Gemini Flash)
#   2. Долгосрочная память (SQLite, сохранение между сессиями)
#   3. Управление ПК через pyautogui (мышь + клавиатура)
#   4. Auto-approve безопасных команд (без Confirmation Gate)
#   5. (фронтенд) Голосовое управление через Web Speech API
#
# Совместимость:
#   - PyInstaller --onefile (freeze_support, resource_path)
#   - asyncio + FastAPI (блокирующие вызовы → asyncio.to_thread)
# ==============================================================

from __future__ import annotations

  # Обязательно для PyInstaller --onefile

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────
# Все сторонние импорты — через try/except для диагностики в .exe
# ──────────────────────────────────────────────────────────────
_IMPORT_ERRORS: list[str] = []

try:
    import httpx
except ImportError as e:
    _IMPORT_ERRORS.append(f"httpx: {e}")

try:
    import uvicorn
except ImportError as e:
    _IMPORT_ERRORS.append(f"uvicorn: {e}")

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, StreamingResponse
except ImportError as e:
    _IMPORT_ERRORS.append(f"fastapi: {e}")

try:
    from pydantic import BaseModel
except ImportError as e:
    _IMPORT_ERRORS.append(f"pydantic: {e}")

try:
    import pyautogui
    # Отключаем защиту от "убегания мыши в угол" (FailSafe),
    # т.к. в десктопном приложении это мешает автоматизации.
    # При необходимости можно вернуть True.
    pyautogui.FAILSAFE = False
    _PYAUTOGUI_AVAILABLE = True
except ImportError as e:
    _IMPORT_ERRORS.append(f"pyautogui: {e}")
    _PYAUTOGUI_AVAILABLE = False


# ──────────────────────────────────────────────────────────────
# Логирование
# ──────────────────────────────────────────────────────────────
def _make_logger() -> logging.Logger:
    if getattr(sys, "frozen", False):
        log_path = Path(sys.executable).parent / "neuralchat.log"
        h: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    else:
        h = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[h],
    )
    return logging.getLogger("neuralchat")

logger = _make_logger()


# ──────────────────────────────────────────────────────────────
# Selftest — проверка импортов при сборке
# ──────────────────────────────────────────────────────────────
def _check_imports() -> None:
    """
    Вызывается при запуске с флагом --selftest.
    Проверяет все критические импорты и пишет результат в лог.
    Используется build.bat для верификации собранного .exe.
    """
    logger.info("=== SELFTEST v5.0: проверка импортов ===")
    all_ok = True

    modules_to_check = [
        ("httpx",           "httpx"),
        ("uvicorn",         "uvicorn"),
        ("fastapi",         "fastapi"),
        ("starlette",       "starlette"),
        ("pydantic",        "pydantic"),
        ("pydantic_core",   "pydantic_core"),
        ("anyio",           "anyio"),
        ("h11",             "h11"),
        ("httpcore",        "httpcore"),
        ("asyncio",         "asyncio"),
        ("sqlite3",         "sqlite3"),      # v5.0: долгосрочная память
        ("pyautogui",       "pyautogui"),    # v5.0: управление ПК
        ("multiprocessing", "multiprocessing"),
    ]

    for display_name, module_name in modules_to_check:
        try:
            mod = __import__(module_name)
            ver = getattr(mod, "__version__", "?")
            logger.info(f"  ✓ {display_name:<20} {ver}")
        except ImportError as exc:
            # pyautogui — некритично, остальное — критично
            if module_name == "pyautogui":
                logger.warning(f"  ⚠ {display_name:<20} отсутствует (ПК-управление отключено): {exc}")
            else:
                logger.error(f"  ✗ {display_name:<20} ОТСУТСТВУЕТ: {exc}")
                all_ok = False

    if all_ok:
        logger.info("=== SELFTEST PASSED ✓ ===")
        print("SELFTEST PASSED")
        sys.exit(0)
    else:
        logger.error("=== SELFTEST FAILED ✗ — см. neuralchat.log ===")
        print("SELFTEST FAILED")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────
# resource_path — корректная работа с файлами внутри .exe
# ──────────────────────────────────────────────────────────────
def resource_path(rel: str) -> Path:
    """Возвращает абсолютный путь к ресурсу.
    В режиме PyInstaller --onefile файлы распаковываются в sys._MEIPASS."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base / rel


# ──────────────────────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────────────────────
OPENROUTER_BASE_URL    = "https://openrouter.ai/api/v1"
CONTEXT_WINDOW_SIZE    = 10      # Сколько последних сообщений отправлять в API
TERMINAL_CMD_TIMEOUT   = 30      # Таймаут выполнения команды (секунды)
SERVER_HOST            = "127.0.0.1"
SERVER_PORT            = 19234
SERVER_STARTUP_TIMEOUT = 20      # Ожидание запуска uvicorn (секунды)

APP_TITLE  = "NeuralChat — AI Assistant"
APP_WIDTH  = 1150
APP_HEIGHT = 820

# ── v5.0: Модели для роутинга оркестратором ──
ORCHESTRATOR_MODEL = "google/gemini-3.1-flash-lite-preview"  # Быстрая модель-диспетчер

# Карта категорий → модели.
# Оркестратор возвращает одну из этих категорий в JSON.
ROUTING_MAP: dict[str, str] = {
    "math":        "deepseek/deepseek-v4-pro",       # Сложная математика
    "coding":      "anthropic/claude-4.6-sonnet",    # Программирование / агентные задачи
    "vision":      "google/gemini-3.1-pro-preview",          # Анализ изображений / скриншотов
    "chat":        "x-ai/grok-4.3",                  # Обычный разговор
}
ROUTING_FALLBACK = "anthropic/claude-4.6-sonnet"     # Если оркестратор вернул мусор             

# Системный промпт для оркестратора.
# Намеренно минималистичный и строгий — только JSON, никаких объяснений.
ORCHESTRATOR_SYSTEM = """You are a strict routing assistant. Analyze the user message and return ONLY a JSON object with one field "category".

Rules:
1. "math"   — complex mathematics, equations, proofs, statistics
2. "coding" — programming, code generation, debugging, terminal/tool use, file operations
3. "vision" — image analysis, screenshots, visual content description
4. "chat"   — general conversation, questions, writing, translation

CRITICAL INSTRUCTION:
Ignore conversational greetings ("привет", "hello", "пожалуйста"). Focus ONLY on the core action.
If the prompt contains an action verb (e.g., "открой", "найди", "напиши", "open", "solve") combined with a tool or application (e.g., "youtube", "browser", "calculator"), YOU MUST classify it as "coding" (tool use), NEVER as "chat".

Response format (ONLY this, no markdown):
{"category": "coding"}"""


# ── v5.0: Паттерны для Auto-approve команд ──
# Команды, соответствующие SAFE_PATTERNS, выполняются без подтверждения.
# Команды, соответствующие DANGEROUS_PATTERNS, всегда требуют подтверждения.

SAFE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*pip\s+install\b",          re.IGNORECASE),  # pip install ...
    re.compile(r"^\s*pip3\s+install\b",         re.IGNORECASE),
    re.compile(r"^\s*npm\s+install\b",          re.IGNORECASE),  # npm install ...
    re.compile(r"^\s*npm\s+i\b",                re.IGNORECASE),
    re.compile(r"^\s*start\b",                  re.IGNORECASE),  # start excel / start ""
    re.compile(r"^\s*calc\s*$",                 re.IGNORECASE),  # открытие калькулятора
    re.compile(r"^\s*notepad\b",                re.IGNORECASE),  # notepad
    re.compile(r"^\s*taskkill\b",               re.IGNORECASE),  # taskkill /F /IM ...
    re.compile(r"^\s*python\s+-m\s+pip\b",      re.IGNORECASE),  # python -m pip ...
    re.compile(r"^\s*echo\b",                   re.IGNORECASE),  # echo (диагностика)
    re.compile(r"^\s*cls\s*$",                  re.IGNORECASE),  # очистка консоли
    re.compile(r"^\s*dir\b",                    re.IGNORECASE),  # просмотр директории
    re.compile(r"^\s*ls\b",                     re.IGNORECASE),
    re.compile(r"^\s*pwd\s*$",                  re.IGNORECASE),
    re.compile(r"^\s*python\s+--version\s*$",   re.IGNORECASE),
    re.compile(r"^\s*python3\s+--version\s*$",  re.IGNORECASE),
    re.compile(r"^\s*node\s+--version\s*$",     re.IGNORECASE),
]

DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\b",        re.IGNORECASE),   # удаление файлов (Unix)
    re.compile(r"\bdel\b",       re.IGNORECASE),   # удаление файлов (Windows)
    re.compile(r"\bformat\b",    re.IGNORECASE),   # форматирование диска
    re.compile(r"\bdrop\b",      re.IGNORECASE),   # DROP TABLE и т.п.
    re.compile(r"\bsudo\b",      re.IGNORECASE),   # суперпользователь
    re.compile(r"\brmdir\b",     re.IGNORECASE),   # удаление директории
    re.compile(r"\brd\s+/s\b",   re.IGNORECASE),   # rd /s (Windows rmdir)
    re.compile(r"\bshutdown\b",  re.IGNORECASE),   # выключение ПК
    re.compile(r"\breboot\b",    re.IGNORECASE),   # перезагрузка
    re.compile(r"\bregdelkey\b", re.IGNORECASE),   # удаление ключей реестра
    re.compile(r"\bdiskpart\b",  re.IGNORECASE),   # управление разделами
    re.compile(r">\s*/dev/",     re.IGNORECASE),   # перезапись системных файлов
    re.compile(r"\bmkfs\b",      re.IGNORECASE),   # форматирование ФС
]


def _classify_command(command: str) -> str:
    """
    Классифицирует команду для терминала.

    Returns:
        "dangerous" — требует Confirmation Gate
        "safe"      — выполняется автоматически (Auto-approve)
        "default"   — стандартное поведение (требует подтверждения)
    """
    # Опасные паттерны имеют приоритет — проверяем первыми
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return "dangerous"

    # Безопасные паттерны — auto-approve
    for pattern in SAFE_PATTERNS:
        if pattern.match(command):
            return "safe"

    # Всё остальное — стандартное подтверждение
    return "default"
# ──────────────────────────────────────────────────────────────
# v5.0: Долгосрочная память — SQLite
# ──────────────────────────────────────────────────────────────

def _get_db_path() -> Path:
    """Возвращает путь к файлу базы данных.
    БД хранится рядом с .exe (или в папке проекта при разработке)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        # При разработке — рядом с app.py
        base = Path(__file__).resolve().parent
    return base / "chats.db"


def _db_init() -> None:
    """Создаёт таблицы в SQLite если их ещё нет.
    Вызывается один раз при старте приложения."""
    db_path = _get_db_path()
    logger.info(f"База данных: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                tool_call_id TEXT  DEFAULT NULL,
                tool_calls   TEXT  DEFAULT NULL,  -- JSON для assistant с tool_calls
                created_at REAL    DEFAULT (unixepoch('now', 'subsec'))
            )
        """)
        # Индекс по session_id для быстрой выборки истории
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages (session_id, id)
        """)
        conn.commit()
    logger.info("БД инициализирована.")


def _db_save_message(session_id: str, msg: dict) -> None:
    """Сохраняет одно сообщение в БД.

    Поддерживает все роли: user, assistant, tool, system.
    tool_calls сохраняется как JSON-строка (для assistant с вызовами инструментов).
    """
    # Если content не строка (например None у assistant с tool_calls) — конвертируем
    content = msg.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)

    tool_calls_json = None
    if msg.get("tool_calls"):
        tool_calls_json = json.dumps(msg["tool_calls"], ensure_ascii=False)

    with sqlite3.connect(_get_db_path()) as conn:
        conn.execute(
            """INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session_id,
                msg.get("role", "user"),
                content,
                msg.get("tool_call_id"),
                tool_calls_json,
            ),
        )
        conn.commit()


def _db_load_history(session_id: str) -> list[dict]:
    """Загружает всю историю сессии из БД.

    Восстанавливает структуру сообщений, понятную OpenRouter API:
    - Обычные сообщения: {"role": ..., "content": ...}
    - Tool messages:     {"role": "tool", "tool_call_id": ..., "content": ...}
    - Assistant с tools: {"role": "assistant", "content": ..., "tool_calls": [...]}
    """
    with sqlite3.connect(_get_db_path()) as conn:
        cursor = conn.execute(
            """SELECT role, content, tool_call_id, tool_calls
               FROM messages
               WHERE session_id = ?
               ORDER BY id ASC""",
            (session_id,),
        )
        rows = cursor.fetchall()

    history: list[dict] = []
    for role, content, tool_call_id, tool_calls_json in rows:
        msg: dict = {"role": role, "content": content}

        if tool_call_id:
            msg["tool_call_id"] = tool_call_id

        if tool_calls_json:
            try:
                msg["tool_calls"] = json.loads(tool_calls_json)
            except json.JSONDecodeError:
                pass  # Если данные повреждены — просто пропускаем

        history.append(msg)

    return history


def _db_clear_history(session_id: str) -> int:
    """Удаляет всю историю сессии из БД. Возвращает количество удалённых записей."""
    with sqlite3.connect(_get_db_path()) as conn:
        cursor = conn.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        return cursor.rowcount


def _db_get_all_sessions() -> list[dict]:
    """Возвращает список всех сессий с метаданными (для фронтенда).
    Используется при загрузке страницы."""
    with sqlite3.connect(_get_db_path()) as conn:
        cursor = conn.execute(
            """SELECT session_id,
                      COUNT(*) as msg_count,
                      MAX(created_at) as last_activity
               FROM messages
               GROUP BY session_id
               ORDER BY last_activity DESC
               LIMIT 50"""
        )
        rows = cursor.fetchall()

    return [
        {"session_id": sid, "msg_count": cnt, "last_activity": last}
        for sid, cnt, last in rows
    ]


# ──────────────────────────────────────────────────────────────
# Состояние агента (in-memory, не требует персистентности)
# ──────────────────────────────────────────────────────────────
# История теперь в SQLite, но для текущей сессии кешируем в памяти
# чтобы не делать запрос к БД на каждой итерации агентского цикла.
_session_cache: dict[str, list[dict]] = {}

# Словари для механизма подтверждения команд
pending_tool_calls:  dict[str, dict]           = {}
tool_call_events:    dict[str, asyncio.Event]  = {}
tool_call_decisions: dict[str, dict]           = {}


def _get_session_history(session_id: str) -> list[dict]:
    """Возвращает историю сессии — из кеша или из БД (при первом обращении)."""
    if session_id not in _session_cache:
        _session_cache[session_id] = _db_load_history(session_id)
    return _session_cache[session_id]


def _append_to_history(session_id: str, msg: dict) -> None:
    """Добавляет сообщение в кеш и одновременно сохраняет в БД."""
    _get_session_history(session_id).append(msg)
    # Сохраняем в БД синхронно — sqlite3 быстрый, не блокирует event loop заметно
    # Для продакшена с высокой нагрузкой стоит перейти на aiosqlite
    _db_save_message(session_id, msg)


# ──────────────────────────────────────────────────────────────
# Pydantic-модели запросов
# ──────────────────────────────────────────────────────────────
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


class ToolDecisionRequest(BaseModel):
    call_id:  str
    approved: bool


class ClearHistoryRequest(BaseModel):
    session_id: str


# ──────────────────────────────────────────────────────────────
# v5.0: Список инструментов (Tools)
# ──────────────────────────────────────────────────────────────
TOOLS: list[dict] = [
    # ── Инструмент 1: Выполнение команд в терминале ──
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

    # ── Инструмент 2: Управление компьютером (мышь + клавиатура) ──
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
                            "Тип действия:\n"
                            "  move         — переместить курсор в (x, y)\n"
                            "  click        — левый клик в (x, y)\n"
                            "  double_click — двойной клик в (x, y)\n"
                            "  right_click  — правый клик в (x, y)\n"
                            "  type_text    — напечатать текст\n"
                            "  press_key    — нажать одну клавишу (enter, tab, esc...)\n"
                            "  hotkey       — нажать комбинацию (ctrl+c, alt+f4...)\n"
                            "  screenshot   — сделать скриншот экрана"
                        ),
                    },
                    "x": {
                        "type": "integer",
                        "description": "Координата X (для move, click, double_click, right_click)."
                    },
                    "y": {
                        "type": "integer",
                        "description": "Координата Y (для move, click, double_click, right_click)."
                    },
                    "text": {
                        "type": "string",
                        "description": "Текст для ввода (для type_text) или клавиша (для press_key)."
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Список клавиш для hotkey (например ['ctrl', 'c'])."
                    },
                    "duration": {
                        "type": "number",
                        "description": "Время движения мыши в секундах (для move, click). По умолчанию 0.2."
                    },
                },
                "required": ["action"],
            },
        },
    },
]


# ──────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────
app = FastAPI(title="NeuralChat", version="5.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────
# Бизнес-логика: вспомогательные функции
# ──────────────────────────────────────────────────────────────

def _sliding_window(session_id: str, system_prompt: str) -> list[dict]:
    """Формирует список сообщений для отправки в API.
    Применяет sliding window для экономии токенов."""
    history = _get_session_history(session_id)
    msgs = [{"role": "system", "content": system_prompt}]
    msgs.extend(history[-CONTEXT_WINDOW_SIZE:])
    return msgs


async def _call_openrouter(
    messages: list[dict],
    api_key:  str,
    model:    str,
    tools:    list[dict] | None = None,
) -> dict:
    """Выполняет запрос к OpenRouter API (не-стримингом).

    Args:
        messages: Список сообщений в формате OpenAI Chat.
        api_key:  Ключ OpenRouter.
        model:    Идентификатор модели.
        tools:    Список инструментов (опционально).

    Returns:
        Полный JSON-ответ от API.
    """
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
    """v5.0: Умный роутинг — определяет нужную модель через оркестратор.

    Отправляет короткий запрос к быстрой модели (Gemini Flash).
    Оркестратор возвращает JSON с категорией, по которой выбирается
    целевая модель из ROUTING_MAP.

    Args:
        message: Текст запроса пользователя.
        api_key: Ключ OpenRouter.

    Returns:
        Идентификатор модели для основного запроса.
    """
    try:
        orchestrator_messages = [
            {"role": "system", "content": ORCHESTRATOR_SYSTEM},
            {"role": "user",   "content": message[:500]},  # Обрезаем для экономии токенов
        ]

        logger.info(f"[Оркестратор] Анализирую запрос: {message[:80]!r}...")

        resp = await _call_openrouter(
            messages=orchestrator_messages,
            api_key=api_key,
            model=ORCHESTRATOR_MODEL,
            tools=None,  # Оркестратору инструменты не нужны
        )

        raw = resp["choices"][0]["message"]["content"].strip()
        logger.info(f"[Оркестратор] Ответ: {raw!r}")

        # Пробуем распарсить JSON
        # Убираем возможные markdown-блоки ```json ... ```
        raw_clean = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
        parsed = json.loads(raw_clean)
        category = parsed.get("category", "chat").lower()

        selected_model = ROUTING_MAP.get(category, ROUTING_FALLBACK)
        logger.info(f"[Оркестратор] Категория: {category!r} → Модель: {selected_model}")
        return selected_model

    except Exception as exc:
        # При любой ошибке оркестратора — используем fallback-модель
        logger.warning(f"[Оркестратор] Ошибка роутинга ({exc}), используем fallback: {ROUTING_FALLBACK}")
        return ROUTING_FALLBACK


async def _run_shell(cmd: str) -> dict:
    """Выполняет shell-команду асинхронно.

    Returns:
        Словарь с полями: success, returncode, stdout, stderr, command.
    """
    logger.info(f"Shell: {cmd!r}")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=TERMINAL_CMD_TIMEOUT,
        )
        rc = proc.returncode
        return {
            "success":    rc == 0,
            "returncode": rc,
            "stdout":     out_b.decode("utf-8", errors="replace").strip() or "(нет вывода)",
            "stderr":     err_b.decode("utf-8", errors="replace").strip(),
            "command":    cmd,
        }
    except asyncio.TimeoutError:
        return {
            "success": False, "returncode": -1,
            "stdout": "", "stderr": f"Таймаут ({TERMINAL_CMD_TIMEOUT}с)", "command": cmd,
        }
    except Exception as exc:
        return {
            "success": False, "returncode": -1,
            "stdout": "", "stderr": str(exc), "command": cmd,
        }


async def _run_computer_control(action: str, **kwargs) -> dict:
    """v5.0: Выполняет действие управления ПК через pyautogui.

    Все вызовы pyautogui выполняются в thread-pool через asyncio.to_thread,
    т.к. pyautogui — синхронная блокирующая библиотека.

    Args:
        action: Тип действия (move, click, type_text и т.д.)
        **kwargs: Параметры действия (x, y, text, keys, duration)

    Returns:
        Словарь с результатом: success, action, details / error.
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {
            "success": False,
            "error": "pyautogui не установлен. Выполните: pip install pyautogui"
        }

    def _blocking_action() -> dict:
        """Синхронная функция — выполняется в отдельном потоке."""
        duration = float(kwargs.get("duration", 0.2))
        x = kwargs.get("x")
        y = kwargs.get("y")

        if action == "move":
            pyautogui.moveTo(x, y, duration=duration)
            return {"success": True, "action": "move", "details": f"Курсор → ({x}, {y})"}

        elif action == "click":
            pyautogui.click(x, y, duration=duration)
            return {"success": True, "action": "click", "details": f"Клик в ({x}, {y})"}

        elif action == "double_click":
            pyautogui.doubleClick(x, y, duration=duration)
            return {"success": True, "action": "double_click", "details": f"Двойной клик в ({x}, {y})"}

        elif action == "right_click":
            pyautogui.rightClick(x, y, duration=duration)
            return {"success": True, "action": "right_click", "details": f"Правый клик в ({x}, {y})"}

        elif action == "type_text":
            text = kwargs.get("text", "")
            # interval=0.05 — небольшая задержка между символами для стабильности
            pyautogui.typewrite(text, interval=0.05)
            return {"success": True, "action": "type_text", "details": f"Напечатано {len(text)} символов"}

        elif action == "press_key":
            key = kwargs.get("text", "enter")
            pyautogui.press(key)
            return {"success": True, "action": "press_key", "details": f"Нажата клавиша: {key}"}

        elif action == "hotkey":
            keys = kwargs.get("keys", [])
            if not keys:
                return {"success": False, "error": "Не указаны клавиши для hotkey"}
            pyautogui.hotkey(*keys)
            return {"success": True, "action": "hotkey", "details": f"Hotkey: {'+'.join(keys)}"}

        elif action == "screenshot":
            # Делаем скриншот и сохраняем во временную папку
            import tempfile
            screenshot_path = Path(tempfile.gettempdir()) / "neuralchat_screenshot.png"
            screenshot = pyautogui.screenshot()
            screenshot.save(str(screenshot_path))
            size = pyautogui.size()
            return {
                "success": True,
                "action":  "screenshot",
                "details": f"Скриншот сохранён: {screenshot_path}",
                "width":   size.width,
                "height":  size.height,
            }

        else:
            return {"success": False, "error": f"Неизвестное действие: {action!r}"}

    try:
        # Запускаем блокирующий pyautogui в thread-pool
        # чтобы не заблокировать event loop FastAPI
        result = await asyncio.to_thread(_blocking_action)
        logger.info(f"[control_computer] {action} → {result.get('details', result.get('error'))}")
        return result
    except Exception as exc:
        logger.error(f"[control_computer] Ошибка: {exc}")
        return {"success": False, "error": str(exc)}


# ──────────────────────────────────────────────────────────────
# Агентский цикл
# ──────────────────────────────────────────────────────────────

async def _agent_loop(
    session_id:    str,
    api_key:       str,
    model:         str,  # Здесь model — результат роутинга оркестратора
    system_prompt: str,
):
    """Основной агентский цикл с поддержкой инструментов.

    Генератор SSE-событий. Каждое событие — строка "data: {...}\\n\\n".

    Поддерживаемые типы событий:
      thinking          — идёт запрос к API
      model_selected    — оркестратор выбрал модель (v5.0)
      tool_request      — запрос на подтверждение команды
      tool_auto_approved — команда выполнена автоматически (v5.0)
      tool_executing    — команда выполняется
      tool_result       — результат выполнения
      tool_rejected     — команда отклонена
      tool_timeout      — таймаут ожидания
      assistant_message — финальный ответ (Markdown)
      usage             — статистика токенов
      error             — ошибка
      done              — цикл завершён
    """
    def sse(t: str, payload: dict) -> str:
        """Форматирует одно SSE-событие."""
        return "data: " + json.dumps({"type": t, **payload}, ensure_ascii=False) + "\n\n"

    for iteration in range(10):  # Максимум 10 итераций агентского цикла
        logger.info(f"[{session_id}] Итерация агента: {iteration + 1}, модель: {model}")
        messages = _sliding_window(session_id, system_prompt)

        try:
            yield sse("thinking", {"message": "Обрабатываю..."})
            resp = await _call_openrouter(messages, api_key, model, TOOLS)
        except httpx.HTTPStatusError as exc:
            yield sse("error", {
                "message": f"Ошибка API ({exc.response.status_code}): {exc.response.text}"
            })
            return
        except Exception as exc:
            yield sse("error", {"message": str(exc)})
            return

        choice = resp["choices"][0]
        finish = choice.get("finish_reason", "")
        msg    = choice["message"]

        # Сохраняем ответ ассистента в историю
        _append_to_history(session_id, msg)

        # ── Финальный ответ (нет вызовов инструментов) ──
        if finish == "stop" or (finish != "tool_calls" and msg.get("content")):
            if content := msg.get("content", ""):
                yield sse("assistant_message", {"content": content})
            if usage := resp.get("usage", {}):
                yield sse("usage", {
                    "prompt_tokens":     usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens":      usage.get("total_tokens", 0),
                    "model":             model,  # v5.0: показываем какая модель ответила
                })
            yield sse("done", {})
            return

        # ── Модель вызывает инструменты ──
        if finish == "tool_calls" or msg.get("tool_calls"):
            for tc in msg.get("tool_calls", []):
                call_id = tc["id"]
                fn_name = tc["function"]["name"]

                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                # ════════════════════════════════════════
                # Инструмент: execute_terminal_command
                # ════════════════════════════════════════
                if fn_name == "execute_terminal_command":
                    command     = fn_args.get("command", "")
                    description = fn_args.get("description", "")
                    cmd_class   = _classify_command(command)

                    logger.info(f"[{session_id}] Команда: {command!r}, класс: {cmd_class}")

                    if cmd_class == "safe":
                        # ── Auto-approve: выполняем без подтверждения ──
                        yield sse("tool_auto_approved", {
                            "call_id":     call_id,
                            "command":     command,
                            "description": description,
                        })
                        yield sse("tool_executing", {"call_id": call_id, "command": command})

                        result      = await _run_shell(command)
                        tool_result = json.dumps(result, ensure_ascii=False)
                        yield sse("tool_result", {"call_id": call_id, "result": result})

                    else:
                        # ── Требуем подтверждения (опасная или неизвестная команда) ──
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
                            "dangerous":   cmd_class == "dangerous",  # v5.0: флаг опасности
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
                                    "command": command,
                                })
                                result      = await _run_shell(command)
                                tool_result = json.dumps(result, ensure_ascii=False)
                                yield sse("tool_result", {
                                    "call_id": call_id,
                                    "result":  result,
                                })
                            else:
                                tool_result = json.dumps({"error": "Пользователь отклонил команду."})
                                yield sse("tool_rejected", {"call_id": call_id})

                        # Чистим словари
                        tool_call_events.pop(call_id, None)
                        pending_tool_calls.pop(call_id, None)

                # ════════════════════════════════════════
                # Инструмент: control_computer (v5.0)
                # ════════════════════════════════════════
                elif fn_name == "control_computer":
                    action = fn_args.get("action", "")

                    # Управление ПК тоже требует подтверждения —
                    # модель не должна кликать куда попало без ведома пользователя.
                    # Исключение: screenshot (безопасно)
                    if action == "screenshot":
                        # Скриншот — безопасен, авто-выполнение
                        yield sse("tool_auto_approved", {
                            "call_id":   call_id,
                            "command":   f"[control_computer] action=screenshot",
                            "description": "Сделать скриншот экрана",
                        })
                        result = await _run_computer_control(action, **fn_args)
                        tool_result = json.dumps(result, ensure_ascii=False)
                        yield sse("tool_result", {"call_id": call_id, "result": result})

                    else:
                        # Остальные действия — через Confirmation Gate
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

                # ════════════════════════════════════════
                # Неизвестный инструмент
                # ════════════════════════════════════════
                else:
                    tool_result = json.dumps({
                        "error": f"Инструмент '{fn_name}' не найден в этой версии."
                    })
                    logger.warning(f"[{session_id}] Неизвестный инструмент: {fn_name!r}")

                # Сохраняем результат вызова инструмента в историю
                _append_to_history(session_id, {
                    "role":         "tool",
                    "tool_call_id": call_id,
                    "content":      tool_result,
                })

            continue  # Следующая итерация агентского цикла

        # Нет ни stop, ни tool_calls — завершаем
        yield sse("done", {})
        return

    yield sse("error", {"message": "Агент превысил лимит итераций (10)."})


def _describe_computer_action(action: str, args: dict) -> str:
    """Создаёт читаемое описание действия для Confirmation Gate."""
    x, y = args.get("x", "?"), args.get("y", "?")
    text = args.get("text", "")
    keys = args.get("keys", [])

    descriptions = {
        "move":         f"Переместить курсор → ({x}, {y})",
        "click":        f"Левый клик в ({x}, {y})",
        "double_click": f"Двойной клик в ({x}, {y})",
        "right_click":  f"Правый клик в ({x}, {y})",
        "type_text":    f"Напечатать текст: {text[:50]!r}",
        "press_key":    f"Нажать клавишу: {text}",
        "hotkey":       f"Горячие клавиши: {'+'.join(keys)}",
    }
    return descriptions.get(action, f"Действие: {action}")
# ──────────────────────────────────────────────────────────────
# HTTP Эндпоинты
# ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Отдаёт HTML-фронтенд."""
    p = resource_path("index.html")
    if not p.exists():
        raise HTTPException(404, f"index.html не найден: {p}")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.post("/chat")
async def chat(req: ChatRequest):
    """Основной эндпоинт чата. Возвращает SSE-поток.

    v5.0: Перед агентским циклом запускает оркестратор для выбора модели.
    """
    # Сохраняем сообщение пользователя в историю (БД + кеш)
    _append_to_history(req.session_id, {"role": "user", "content": req.message})

    # Определяем модель через оркестратор
    selected_model = await _route_model(req.message, req.api_key)

    async def stream_with_model_event():
        """Оборачиваем агентский цикл: сначала сообщаем о выбранной модели."""
        # Сообщаем фронтенду какую модель выбрал оркестратор
        yield "data: " + json.dumps({
            "type":  "model_selected",
            "model": selected_model,
        }, ensure_ascii=False) + "\n\n"

        # Запускаем основной агентский цикл
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


@app.post("/tool-decision")
async def tool_decision(req: ToolDecisionRequest):
    """Принимает решение пользователя по Confirmation Gate."""
    if req.call_id not in tool_call_events:
        raise HTTPException(404, f"call_id {req.call_id!r} не найден или уже обработан")
    tool_call_decisions[req.call_id] = {"approved": req.approved}
    tool_call_events[req.call_id].set()
    return {"ok": True}


@app.post("/clear-history")
async def clear_history(req: ClearHistoryRequest):
    """Очищает историю сессии (в кеше и в БД)."""
    # Очищаем кеш в памяти
    _session_cache.pop(req.session_id, None)
    # Очищаем в БД (в thread-pool, т.к. sqlite3 синхронный)
    n = await asyncio.to_thread(_db_clear_history, req.session_id)
    return {"ok": True, "cleared": n}


@app.get("/history/{session_id}")
async def get_history(session_id: str):
    """v5.0: Возвращает историю сессии для восстановления чата при перезагрузке.

    Фронтенд вызывает этот эндпоинт при загрузке страницы.
    """
    history = await asyncio.to_thread(_db_load_history, session_id)

    # Фильтруем: фронтенду нужны только user и assistant сообщения
    # (tool-сообщения — внутренние, для контекста модели)
    visible = [
        msg for msg in history
        if msg["role"] in ("user", "assistant") and msg.get("content")
    ]
    return {"session_id": session_id, "messages": visible}


@app.get("/sessions")
async def get_sessions():
    """v5.0: Возвращает список всех сохранённых сессий."""
    sessions = await asyncio.to_thread(_db_get_all_sessions)
    return {"sessions": sessions}


@app.get("/health")
async def health():
    """Health-check эндпоинт."""
    return {
        "status":           "ok",
        "version":          "5.0.0",
        "pyautogui":        _PYAUTOGUI_AVAILABLE,
        "db_path":          str(_get_db_path()),
    }


# ──────────────────────────────────────────────────────────────
# Uvicorn в фоновом потоке
# ──────────────────────────────────────────────────────────────

def _start_server() -> None:
    try:
        import traceback
        config = uvicorn.Config(
            app=app,
            host=SERVER_HOST,
            port=SERVER_PORT,
            log_config=None,  # Отключаем капризные логи Uvicorn внутри .exe
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        server.run()
    except Exception as e:
        import traceback
        # Пишем ошибку красным в консоль и в neuralchat.log
        err_msg = f"UVICORN FATAL ERROR:\n{traceback.format_exc()}"
        logger.critical(err_msg)
        print("\n" + "="*50)
        print(err_msg)
        print("="*50 + "\n")


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Ждёт пока порт станет доступен. Возвращает True при успехе."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ──────────────────────────────────────────────────────────────
# Запуск браузера в режиме --app (нативное окно)
# ──────────────────────────────────────────────────────────────

_BROWSER_PATHS_WIN = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]


def _find_browser() -> str | None:
    for p in _BROWSER_PATHS_WIN:
        expanded = os.path.expandvars(p)
        if os.path.isfile(expanded):
            logger.info(f"Браузер найден: {expanded}")
            return expanded
    for name in ("msedge", "google-chrome", "chromium", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _get_profile_dir() -> str:
    """Возвращает путь к профилю браузера.
    Профиль хранится рядом с .exe (или в APPDATA при разработке)."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent / "browser_profile"
    else:
        appdata = os.environ.get("APPDATA", "")
        base = (
            Path(appdata) / "NeuralChat" / "profile"
            if appdata
            else Path(__file__).parent / "browser_profile"
        )
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def _open_app_window(url: str) -> subprocess.Popen | None:
    """Открывает приложение в окне браузера без адресной строки (--app mode)."""
    browser = _find_browser()
    if not browser:
        return None

    profile = _get_profile_dir()
    cmd = [
        browser,
        f"--app={url}",
        f"--window-size={APP_WIDTH},{APP_HEIGHT}",
        "--window-position=80,40",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-translate",
        "--disable-features=TranslateUI",
    ]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        return subprocess.Popen(
            cmd,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.error(f"Ошибка запуска браузера: {exc}")
        return None


def _watch_browser(proc: subprocess.Popen) -> None:
    """Daemon-поток: следит за браузером и завершает приложение при его закрытии."""
    proc.wait()
    logger.info("Браузер закрыт → завершаем приложение.")
    time.sleep(0.3)
        # Небольшая задержка, чтобы избежать гонки при закрытии приложения и браузера


# ──────────────────────────────────────────────────────────────
# main()
# ──────────────────────────────────────────────────────────────

def main() -> None:
    # ── Режим самодиагностики (build.bat) ──
    if "--selftest" in sys.argv:
        _check_imports()
        return  # sys.exit вызывается внутри _check_imports()

    # ── Критические ошибки импорта ──
    if _IMPORT_ERRORS:
        for err in _IMPORT_ERRORS:
            # pyautogui — некритично
            if "pyautogui" not in err:
                logger.error(f"Критический импорт отсутствует: {err}")

        # Проверяем — есть ли критические (не-pyautogui) ошибки
        critical_errors = [e for e in _IMPORT_ERRORS if "pyautogui" not in e]
        if critical_errors:
            if getattr(sys, "frozen", False):
                err_file = Path(sys.executable).parent / "STARTUP_ERROR.txt"
                err_file.write_text(
                    "NeuralChat v5.0 не запустился из-за отсутствующих модулей:\n\n"
                    + "\n".join(critical_errors)
                    + "\n\nРешение: пересоберите .exe через build.bat\n",
                    encoding="utf-8",
                )
            sys.exit(1)

    logger.info("=" * 60)
    logger.info("  NeuralChat Desktop v5.0 запускается")
    logger.info(f"  http://{SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"  pyautogui: {'✓' if _PYAUTOGUI_AVAILABLE else '✗ (отключено)'}")
    logger.info(f"  База данных: {_get_db_path()}")
    logger.info("=" * 60)

    # 1. Инициализируем БД (создаём таблицы если нет)
    _db_init()

    # 2. Запускаем сервер в фоновом потоке
    threading.Thread(target=_start_server, daemon=True, name="uvicorn").start()

    # 3. Ждём готовности порта
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/"
    if not _wait_for_port(SERVER_HOST, SERVER_PORT, SERVER_STARTUP_TIMEOUT):
        logger.error("Сервер не запустился за отведённое время!")
        sys.exit(1)
    logger.info("✓ Сервер готов.")

    # 4. Открываем окно браузера
    proc = _open_app_window(url)

    if proc is None:
        logger.warning("Chromium-браузер не найден → открываем системный браузер.")
        webbrowser.open(url)
        print(f"\n✅ NeuralChat v5.0: {url}\n   Ctrl+C для выхода.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        sys.exit(0)

    # 5. Следим за процессом браузера
    threading.Thread(
        target=_watch_browser,
        args=(proc,),
        daemon=True,
        name="browser-watcher",
    ).start()

    logger.info(f"✓ Приложение запущено. PID браузера: {proc.pid}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Ctrl+C — завершение.")
        proc.terminate()
        sys.exit(0)

import multiprocessing
if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()