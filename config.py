import re

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

ROUTING_MAP: dict[str, str] = {
    "math":        "deepseek/deepseek-v4-pro",       # Сложная математика
    "coding":      "anthropic/claude-4.6-sonnet",    # Программирование / агентные задачи
    "vision":      "google/gemini-3.1-pro-preview",          # Анализ изображений / скриншотов
    "chat":        "x-ai/grok-4.3",                  # Обычный разговор
    "image_generation": "image_generation",          # Генерация изображений (перехватывается локально)
}
ROUTING_FALLBACK = "anthropic/claude-4.6-sonnet"     # Если оркестратор вернул мусор

ORCHESTRATOR_SYSTEM = """You are a strict routing assistant. Analyze the user message and return ONLY a JSON object with one field "category".

Rules:
1. "math"   — complex mathematics, equations, proofs, statistics
2. "coding" — programming, code generation, debugging, terminal/tool use, file operations
3. "vision" — image analysis, screenshots, visual content description
4. "chat"   — general conversation, questions, writing, translation
5. "image_generation" — intent to generate, draw, or create an image (triggers: "нарисуй", "создай изображение", "сгенерируй картинку", "draw", "generate image")

CRITICAL INSTRUCTION:
Ignore conversational greetings ("привет", "hello", "пожалуйста"). Focus ONLY on the core action.
If the prompt contains an action verb (e.g., "открой", "найди", "напиши", "open", "solve") combined with a tool or application (e.g., "youtube", "browser", "calculator"), YOU MUST classify it as "coding" (tool use), NEVER as "chat".

Response format (ONLY this, no markdown):
{"category": "coding"}"""

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
