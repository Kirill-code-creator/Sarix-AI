import asyncio
import tempfile
from pathlib import Path
from logger import logger

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    _PYAUTOGUI_AVAILABLE = True
except Exception:
    _PYAUTOGUI_AVAILABLE = False

async def _run_computer_control(action: str, **kwargs) -> dict:
    if not _PYAUTOGUI_AVAILABLE:
        return {
            "success": False,
            "error": "pyautogui не установлен. Выполните: pip install pyautogui"
        }

    def _blocking_action() -> dict:
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
        result = await asyncio.to_thread(_blocking_action)
        logger.info(f"[control_computer] {action} → {result.get('details', result.get('error'))}")
        return result
    except Exception as exc:
        logger.error(f"[control_computer] Ошибка: {exc}")
        return {"success": False, "error": str(exc)}

def _describe_computer_action(action: str, args: dict) -> str:
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
