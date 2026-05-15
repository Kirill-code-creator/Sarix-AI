from fastapi import APIRouter
from services.computer import _PYAUTOGUI_AVAILABLE
from database import _get_db_path

router = APIRouter()

@router.get("/health")
async def health():
    return {
        "status":           "ok",
        "version":          "5.0.0",
        "pyautogui":        _PYAUTOGUI_AVAILABLE,
        "db_path":          str(_get_db_path()),
    }
