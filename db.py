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
        # Журнал доган (попереджень)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                offense               TEXT,
                date_text             TEXT,
                to_whom               TEXT,
                rank_to               TEXT,
                by_whom               TEXT,
                kind                  TEXT, -- 'Догана' або 'Попередження'
                issued_by_user_id     INTEGER,
                issued_by_username    TEXT,
                created_at            TEXT DEFAULT (datetime('now')),
                revoked_at            TEXT,
                revoked_by_user_id    INTEGER,
                revoked_by_name       TEXT,
                revoke_reason         TEXT
            );
            """
        )
        # Журнал заяв на неактив
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS neaktyv_requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id        INTEGER NOT NULL,
                requester_username  TEXT,
                to_whom             TEXT,
                rank                TEXT,
                duration            TEXT,
                department          TEXT,
                status              TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending',
                moderator_name      TEXT,
                moderator_user_id   INTEGER,
                decided_at          TEXT,
                created_at          TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Журнал заяв на доступ у групу та рішень по ним
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS access_applications (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id               INTEGER NOT NULL,
                username              TEXT,
                in_game_name          TEXT,
                npu_department        TEXT,
                rank                  TEXT,
                images                TEXT,
                created_at            TEXT DEFAULT (datetime('now')),
                decision              TEXT CHECK(decision IN ('pending','approved','rejected')) DEFAULT 'pending',
                decided_at            TEXT,
                decided_by_admin_id   INTEGER,
                decided_by_username   TEXT,
                invite_link           TEXT
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


# ======= Warnings (Догани) =======
def insert_warning(
    offense: str,
    date_text: str,
    to_whom: str,
    rank_to: str | None,
    by_whom: str,
    kind: str,
    issued_by_user_id: int | None,
    issued_by_username: str | None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO warnings (offense, date_text, to_whom, rank_to, by_whom, kind, issued_by_user_id, issued_by_username)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (offense, date_text, to_whom, rank_to, by_whom, kind, issued_by_user_id, issued_by_username),
        )
        return int(cur.lastrowid)


def revoke_warning(warning_id: int, revoked_by_user_id: int, revoked_by_name: str, reason: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE warnings
            SET revoked_at = datetime('now'), revoked_by_user_id = ?, revoked_by_name = ?, revoke_reason = ?
            WHERE id = ?
            """,
            (revoked_by_user_id, revoked_by_name, reason, warning_id),
        )


# ======= Neaktyv =======
def insert_neaktyv_request(
    requester_id: int,
    requester_username: str | None,
    to_whom: str,
    rank: str | None,
    duration: str,
    department: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO neaktyv_requests (requester_id, requester_username, to_whom, rank, duration, department)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (requester_id, requester_username, to_whom, rank, duration, department),
        )
        return int(cur.lastrowid)


def decide_neaktyv_request(
    request_id: int,
    status: str,
    moderator_name: str,
    moderator_user_id: int,
):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE neaktyv_requests
            SET status = ?, moderator_name = ?, moderator_user_id = ?, decided_at = datetime('now')
            WHERE id = ?
            """,
            (status, moderator_name, moderator_user_id, request_id),
        )


# ======= Access Applications =======
def insert_access_application(
    user_id: int,
    username: str | None,
    in_game_name: str | None,
    npu_department: str | None,
    rank: str | None,
    images: list[str] | None,
) -> int:
    imgs = "\n".join(images or [])
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO access_applications (user_id, username, in_game_name, npu_department, rank, images)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, in_game_name, npu_department, rank, imgs),
        )
        return int(cur.lastrowid)


def decide_access_application(
    user_id: int,
    decision: str,
    decided_by_admin_id: int,
    decided_by_username: str | None,
    invite_link: str | None,
):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE access_applications
            SET decision = ?, decided_at = datetime('now'), decided_by_admin_id = ?, decided_by_username = ?, invite_link = ?
            WHERE id = (
                SELECT id FROM access_applications WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
            )
            """,
            (decision, decided_by_admin_id, decided_by_username, invite_link, user_id),
        )
