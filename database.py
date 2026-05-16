import json
import sys
from pathlib import Path
import aiosqlite
from logger import logger

def _get_db_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    return base / "chats.db"

async def _db_init() -> None:
    db_path = _get_db_path()
    logger.info(f"База данных: {db_path}")

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                tool_call_id TEXT  DEFAULT NULL,
                tool_calls   TEXT  DEFAULT NULL,
                created_at REAL    DEFAULT (unixepoch('now', 'subsec'))
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages (session_id, id)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact TEXT NOT NULL,
                created_at REAL DEFAULT (unixepoch('now', 'subsec'))
            )
        """)
        await conn.commit()
    logger.info("БД инициализирована.")

async def _db_save_message(session_id: str, msg: dict) -> None:
    content = msg.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)

    tool_calls_json = None
    if msg.get("tool_calls"):
        tool_calls_json = json.dumps(msg["tool_calls"], ensure_ascii=False)

    async with aiosqlite.connect(_get_db_path()) as conn:
        await conn.execute(
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
        await conn.commit()

async def _db_load_history(session_id: str) -> list[dict]:
    async with aiosqlite.connect(_get_db_path()) as conn:
        async with conn.execute(
            """SELECT role, content, tool_call_id, tool_calls
               FROM messages
               WHERE session_id = ?
               ORDER BY id ASC""",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()

    history: list[dict] = []
    for role, content, tool_call_id, tool_calls_json in rows:
        msg: dict = {"role": role, "content": content}

        if tool_call_id:
            msg["tool_call_id"] = tool_call_id

        if tool_calls_json:
            try:
                msg["tool_calls"] = json.loads(tool_calls_json)
            except json.JSONDecodeError:
                pass

        history.append(msg)

    return history

async def _db_clear_history(session_id: str) -> int:
    async with aiosqlite.connect(_get_db_path()) as conn:
        cursor = await conn.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (session_id,),
        )
        await conn.commit()
        return cursor.rowcount

async def _db_get_all_sessions() -> list[dict]:
    async with aiosqlite.connect(_get_db_path()) as conn:
        async with conn.execute(
            """SELECT session_id,
                      COUNT(*) as msg_count,
                      MAX(created_at) as last_activity
               FROM messages
               GROUP BY session_id
               ORDER BY last_activity DESC
               LIMIT 50"""
        ) as cursor:
            rows = await cursor.fetchall()

    return [
        {"session_id": sid, "msg_count": cnt, "last_activity": last}
        for sid, cnt, last in rows
    ]

async def _db_save_memory(fact: str) -> None:
    async with aiosqlite.connect(_get_db_path()) as conn:
        await conn.execute("INSERT INTO user_memory (fact) VALUES (?)", (fact,))
        await conn.commit()

async def _db_load_memory() -> list[str]:
    async with aiosqlite.connect(_get_db_path()) as conn:
        async with conn.execute("SELECT fact FROM user_memory ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows]
