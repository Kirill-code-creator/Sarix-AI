import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from config import SERVER_HOST, SERVER_PORT, SERVER_STARTUP_TIMEOUT, APP_WIDTH, APP_HEIGHT
from logger import logger, _make_logger
from database import _db_init, _get_db_path
from services.computer import _PYAUTOGUI_AVAILABLE
import routers.chat
import routers.history
import routers.health

# ──────────────────────────────────────────────────────────────
# resource_path — корректная работа с файлами внутри .exe
# ──────────────────────────────────────────────────────────────
def resource_path(rel: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base / rel

# ──────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB asynchronously before handling requests
    await _db_init()
    yield
    # Cleanup (if needed)

app = FastAPI(title="NeuralChat", version="5.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routers.chat.router)
app.include_router(routers.history.router)
app.include_router(routers.health.router)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    p = resource_path("index.html")
    if not p.exists():
        raise HTTPException(404, f"index.html не найден: {p}")
    return HTMLResponse(p.read_text(encoding="utf-8"))

# ──────────────────────────────────────────────────────────────
# Selftest
# ──────────────────────────────────────────────────────────────
def _check_imports() -> None:
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
        ("aiosqlite",       "aiosqlite"),
        ("pyautogui",       "pyautogui"),
        ("multiprocessing", "multiprocessing"),
    ]

    for display_name, module_name in modules_to_check:
        try:
            mod = __import__(module_name)
            ver = getattr(mod, "__version__", "?")
            logger.info(f"  ✓ {display_name:<20} {ver}")
        except ImportError as exc:
            if module_name == "pyautogui":
                logger.warning(f"  ⚠ {display_name:<20} отсутствует: {exc}")
            else:
                logger.error(f"  ✗ {display_name:<20} ОТСУТСТВУЕТ: {exc}")
                all_ok = False

    if all_ok:
        logger.info("=== SELFTEST PASSED ✓ ===")
        print("SELFTEST PASSED")
        sys.exit(0)
    else:
        logger.error("=== SELFTEST FAILED ✗ ===")
        print("SELFTEST FAILED")
        sys.exit(1)

# ──────────────────────────────────────────────────────────────
# Uvicorn в фоновом потоке
# ──────────────────────────────────────────────────────────────

def _start_server() -> None:
    try:
        config = uvicorn.Config(
            app=app,
            host=SERVER_HOST,
            port=SERVER_PORT,
            log_config=None,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        server.run()
    except Exception as e:
        import traceback
        err_msg = f"UVICORN FATAL ERROR:\n{traceback.format_exc()}"
        logger.critical(err_msg)
        print("\n" + "="*50)
        print(err_msg)
        print("="*50 + "\n")

def _wait_for_port(host: str, port: int, timeout: float) -> bool:
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
            return expanded
    for name in ("msedge", "google-chrome", "chromium", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None

def _get_profile_dir() -> str:
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
    proc.wait()
    logger.info("Браузер закрыт → завершаем приложение.")
    time.sleep(0.3)
    os._exit(0)

# ──────────────────────────────────────────────────────────────
# main()
# ──────────────────────────────────────────────────────────────

def main() -> None:
    if "--selftest" in sys.argv:
        _check_imports()
        return

    logger.info("=" * 60)
    logger.info("  NeuralChat Desktop v5.0 запускается")
    logger.info(f"  http://{SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"  pyautogui: {'✓' if _PYAUTOGUI_AVAILABLE else '✗ (отключено)'}")
    logger.info(f"  База данных: {_get_db_path()}")
    logger.info("=" * 60)

    threading.Thread(target=_start_server, daemon=True, name="uvicorn").start()

    url = f"http://{SERVER_HOST}:{SERVER_PORT}/"
    if not _wait_for_port(SERVER_HOST, SERVER_PORT, SERVER_STARTUP_TIMEOUT):
        logger.error("Сервер не запустился за отведённое время!")
        sys.exit(1)
    logger.info("✓ Сервер готов.")

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
