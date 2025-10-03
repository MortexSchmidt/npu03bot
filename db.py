import os
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, Any

# Разрешаем переопределять путь к БД через переменные окружения
_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_ENV_DB_PATH = os.getenv("DB_PATH")
_ENV_DB_DIR = os.getenv("DB_DIR")

if _ENV_DB_PATH:
    DB_PATH = _ENV_DB_PATH
else:
    data_dir = _ENV_DB_DIR or _DEFAULT_DATA_DIR
    DB_PATH = os.path.join(data_dir, "bot.db")


def _ensure_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                telegram_id     INTEGER PRIMARY KEY,
                username        TEXT,
                full_name_tg    TEXT,
                in_game_name    TEXT,
                rank            TEXT,
                npu_department  TEXT,
                role            TEXT CHECK(role IN ('user','admin')) DEFAULT 'user',
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_images (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL,
                url          TEXT NOT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(telegram_id) REFERENCES profiles(telegram_id) ON DELETE CASCADE
            );
            """
        )


def upsert_profile(
    telegram_id: int,
    username: Optional[str] = None,
    full_name_tg: Optional[str] = None,
    in_game_name: Optional[str] = None,
    rank: Optional[str] = None,
    npu_department: Optional[str] = None,
    role: Optional[str] = None,
):
    """Вставляет или обновляет профиль. Только переданные поля будут обновлены."""
    fields: Dict[str, Any] = {
        "telegram_id": telegram_id,
    }
    if username is not None:
        fields["username"] = username
    if full_name_tg is not None:
        fields["full_name_tg"] = full_name_tg
    if in_game_name is not None:
        fields["in_game_name"] = in_game_name
    if rank is not None:
        fields["rank"] = rank
    if npu_department is not None:
        fields["npu_department"] = npu_department
    if role is not None:
        fields["role"] = role

    # Строим UPSERT динамически по переданным полям, обновляя updated_at
    cols = ", ".join(fields.keys())
    placeholders = ", ".join([":" + k for k in fields.keys()])
    set_parts = [f"{k}=excluded.{k}" for k in fields.keys() if k != "telegram_id"]
    set_sql = ", ".join(set_parts + ["updated_at = datetime('now')"]) if set_parts else "updated_at = datetime('now')"

    sql = f"""
        INSERT INTO profiles ({cols})
        VALUES ({placeholders})
        ON CONFLICT(telegram_id) DO UPDATE SET {set_sql};
    """

    with get_conn() as conn:
        conn.execute(sql, fields)


def update_profile_fields(telegram_id: int, **fields):
    if not fields:
        return
    set_sql = ", ".join([f"{k} = :{k}" for k in fields.keys()])
    params = {"telegram_id": telegram_id, **fields}
    with get_conn() as conn:
        conn.execute(
            f"UPDATE profiles SET {set_sql}, updated_at = datetime('now') WHERE telegram_id = :telegram_id",
            params,
        )


def get_profile(telegram_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT telegram_id, username, full_name_tg, in_game_name, rank, npu_department, role, created_at, updated_at FROM profiles WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = [
            "telegram_id",
            "username",
            "full_name_tg",
            "in_game_name",
            "rank",
            "npu_department",
            "role",
            "created_at",
            "updated_at",
        ]
        return dict(zip(keys, row))


def get_profile_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Получить профиль по @username (без учёта регистра, @ необязателен)."""
    uname = (username or "").lstrip("@").strip()
    if not uname:
        return None
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT telegram_id, username, full_name_tg, in_game_name, rank, npu_department, role, created_at, updated_at
            FROM profiles WHERE lower(username) = lower(?)
            """,
            (uname,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = [
            "telegram_id","username","full_name_tg","in_game_name","rank","npu_department","role","created_at","updated_at",
        ]
        return dict(zip(keys, row))


def search_profiles(query: str, limit: int = 10) -> list[Dict[str, Any]]:
    """Полнотекстовый простой поиск по username, full_name_tg, in_game_name (LIKE, без регистра)."""
    q = f"%{(query or '').strip()}%"
    if q == "%%":
        return []
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT telegram_id, username, full_name_tg, in_game_name, rank, npu_department, role, created_at, updated_at
            FROM profiles
            WHERE lower(coalesce(username,'')) LIKE lower(?)
               OR lower(coalesce(full_name_tg,'')) LIKE lower(?)
               OR lower(coalesce(in_game_name,'')) LIKE lower(?)
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (q, q, q, limit),
        )
        rows = cur.fetchall()
        keys = [
            "telegram_id","username","full_name_tg","in_game_name","rank","npu_department","role","created_at","updated_at",
        ]
        return [dict(zip(keys, row)) for row in rows]


def replace_profile_images(telegram_id: int, urls: list[str]):
    """Полностью заменяет список изображений профиля на переданный."""
    with get_conn() as conn:
        conn.execute("DELETE FROM profile_images WHERE telegram_id = ?", (telegram_id,))
        if urls:
            conn.executemany(
                "INSERT INTO profile_images(telegram_id, url) VALUES(?, ?)",
                [(telegram_id, u) for u in urls],
            )


def get_profile_images(telegram_id: int) -> list[str]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT url FROM profile_images WHERE telegram_id = ? ORDER BY id ASC",
            (telegram_id,),
        )
        return [r[0] for r in cur.fetchall()]
