import asyncio
import shlex
import re
from typing import AsyncGenerator
from logger import logger
from config import SAFE_PATTERNS, DANGEROUS_PATTERNS

def _classify_command(command: str) -> str:
    """
    Классифицирует команду для терминала.
    Использует shlex для предотвращения shell-инъекций.
    """
    try:
        # Пытаемся разбить команду с помощью shlex (работает как парсер POSIX shell)
        parts = shlex.split(command)
    except ValueError:
        # Если shlex не может распарсить (например, незакрытые кавычки),
        # считаем команду потенциально опасной, так как не можем её безопасно проанализировать.
        return "dangerous"

    # Проверяем каждую часть команды (слова) на опасные ключевые слова (например, "rm", "&&", ";")
    for part in parts:
        # Если в команде есть операторы цепочки или перенаправления,
        # считаем её опасной для безопасности.
        if part in ("&&", "||", ";", "|", ">", ">>", "<"):
             return "dangerous"
        # Также проверяем по регуляркам опасных паттернов
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(part):
                return "dangerous"

    # Если опасных паттернов не найдено, проверим безопасные
    # shlex.split сохраняет структуру, мы проверяем оригинальную команду
    # или хотя бы первую часть команды.
    # Так как мы ищем по всей строке, просто используем старую логику поверх.
    # Но для безопасности проверяем ещё раз всю команду.
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return "dangerous"

    for pattern in SAFE_PATTERNS:
        if pattern.match(command):
            return "safe"

    return "default"

async def _run_shell_stream(cmd: str, timeout: int = 30) -> AsyncGenerator[str, None]:
    """
    Выполняет shell-команду и возвращает вывод асинхронным генератором построчно.
    """
    logger.info(f"Shell stream: {cmd!r}")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Читаем построчно
        if proc.stdout:
            while True:
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                    if not line_bytes:
                        break
                    line = line_bytes.decode('utf-8', errors='replace')
                    yield line
                except asyncio.TimeoutError:
                    yield f"\n[Ошибка: Превышено время ожидания ({timeout}с)]\n"
                    proc.kill()
                    break

        await proc.wait()

    except Exception as exc:
         yield f"\n[Ошибка выполнения: {exc}]\n"
