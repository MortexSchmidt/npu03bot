import os
import sqlite3
import csv
import io
import traceback
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
                file_id      TEXT NOT NULL,
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
        # Загальний журнал дій адміністраторів
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_logs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id         INTEGER,
                actor_username   TEXT,
                action           TEXT,
                target_user_id   INTEGER,
                target_username  TEXT,
                details          TEXT,
                created_at       TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Журнал оновлень профілю (через /refill або інші дії)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_updates (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER NOT NULL,
                fields         TEXT, -- JSON/текст із переліком оновлених полів
                images_count   INTEGER,
                source         TEXT, -- 'refill' | 'apply' | ...
                created_at     TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Події антиспаму
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS antispam_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                kind         TEXT CHECK(kind IN ('message','callback')),
                retry_after  REAL,
                created_at   TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Логи ошибок
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS error_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type   TEXT,
                message      TEXT,
                stack        TEXT,
                update_json  TEXT,
                context_info TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Заявки на повышение
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promotion_requests (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id          INTEGER NOT NULL,
                requester_username    TEXT,
                requester_name        TEXT,
                current_rank          TEXT NOT NULL,
                target_rank           TEXT NOT NULL,
                workbook_image_id     TEXT NOT NULL,
                work_evidence_image_ids TEXT NOT NULL,
                status                TEXT CHECK(status IN ('pending','approved','rejected')) DEFAULT 'pending',
                moderator_id          INTEGER,
                moderator_username    TEXT,
                moderator_rank        TEXT,
                reject_reason         TEXT,
                decided_at            TEXT,
                created_at            TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(requester_id) REFERENCES profiles(telegram_id) ON DELETE CASCADE
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


def replace_profile_images(telegram_id: int, file_ids: list[str]):
    """Полностью заменяет список изображений профиля на переданный."""
    with get_conn() as conn:
        conn.execute("DELETE FROM profile_images WHERE telegram_id = ?", (telegram_id,))
        if file_ids:
            conn.executemany(
                "INSERT INTO profile_images(telegram_id, file_id) VALUES(?, ?)",
                [(telegram_id, u) for u in file_ids],
            )


def get_profile_images(telegram_id: int) -> list[str]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT file_id FROM profile_images WHERE telegram_id = ? ORDER BY id ASC",
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


# ===== Загальні логи дій =====
def log_action(
    actor_id: int | None,
    actor_username: str | None,
    action: str,
    target_user_id: int | None = None,
    target_username: str | None = None,
    details: str | None = None,
):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO action_logs (actor_id, actor_username, action, target_user_id, target_username, details)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (actor_id, actor_username, action, target_user_id, target_username, details),
        )


def log_profile_update(
    user_id: int,
    fields: dict[str, Any] | None,
    images_count: int | None,
    source: str,
):
    # Просте текстове подання полів
    fields_text = None
    if fields:
        try:
            # Без json, щоб не тягнути залежності: простий key=value;...
            fields_text = "; ".join([f"{k}={v}" for k, v in fields.items()])
        except Exception:
            fields_text = str(fields)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO profile_updates (user_id, fields, images_count, source)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, fields_text, images_count, source),
        )


def log_antispam_event(user_id: int, kind: str, retry_after: float | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO antispam_events (user_id, kind, retry_after)
            VALUES (?, ?, ?)
            """,
            (user_id, kind, retry_after),
        )


# ===== Логи ошибок =====
def log_error(error_type: str | None, message: str | None, stack: str | None, update_json: str | None, context_info: str | None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO error_logs (error_type, message, stack, update_json, context_info)
            VALUES (?, ?, ?, ?, ?)
            """,
            (error_type, message, stack, update_json, context_info),
        )


# ===== Запросы/сводки для админов =====
def query_action_logs(
    limit: int = 50,
    actor_id: int | None = None,
    actor_username: str | None = None,
    action: str | None = None,
    date_from: str | None = None,  # 'YYYY-MM-DD'
    date_to: str | None = None,    # 'YYYY-MM-DD'
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []
    if actor_id is not None:
        where.append("actor_id = ?")
        params.append(actor_id)
    if actor_username:
        where.append("lower(coalesce(actor_username,'')) = lower(?)")
        params.append(actor_username.lstrip('@'))
    if action:
        where.append("action = ?")
        params.append(action)
    if date_from:
        where.append("date(created_at) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(created_at) <= date(?)")
        params.append(date_to)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, actor_id, actor_username, action, target_user_id, target_username, details, created_at
        FROM action_logs
        {where_sql}
        ORDER BY id DESC
        LIMIT ?
    """
    params.append(limit)
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        keys = ["id","actor_id","actor_username","action","target_user_id","target_username","details","created_at"]
        return [dict(zip(keys, r)) for r in rows]


def query_antispam_top(days: int = 7, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    where = ["datetime(created_at) >= datetime('now', ?) "]
    params: list[Any] = [f"-{int(days)} days"]
    if kind in ("message", "callback"):
        where.append("kind = ?")
        params.append(kind)
    where_sql = " WHERE " + " AND ".join(where)
    sql = f"""
        SELECT user_id, COUNT(*) AS cnt
        FROM antispam_events
        {where_sql}
        GROUP BY user_id
        ORDER BY cnt DESC
        LIMIT ?
    """
    params.append(limit)
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return [{"user_id": r[0], "count": r[1]} for r in cur.fetchall()]


def _table_primary_timestamp(table: str) -> str | None:
    # Определяем подходящую дату для фильтра по дням
    candidates = {
        "action_logs": "created_at",
        "profile_updates": "created_at",
        "antispam_events": "created_at",
        "warnings": "created_at",
        "neaktyv_requests": "created_at",
        "access_applications": "created_at",
        "error_logs": "created_at",
        "profiles": "updated_at",
        "profile_images": "created_at",
    }
    return candidates.get(table)


def export_table_csv(table: str, days: int | None = None) -> tuple[str, bytes]:
    """Экспорт таблицы в CSV. Возвращает (filename, bytes). Разрешены только известные таблицы."""
    allowed = {
        "profiles", "profile_images", "warnings", "neaktyv_requests",
        "access_applications", "action_logs", "profile_updates", "antispam_events", "error_logs"
    }
    if table not in allowed:
        raise ValueError("Недопустима таблиця для експорту")
    ts_col = _table_primary_timestamp(table)
    where_sql = ""
    params: list[Any] = []
    if days and ts_col:
        where_sql = f" WHERE datetime({ts_col}) >= datetime('now', ?)"
        params.append(f"-{int(days)} days")
    # Получаем список столбцов
    with get_conn() as conn:
        cur = conn.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        select_sql = f"SELECT {', '.join(cols)} FROM {table}{where_sql}"
        cur2 = conn.execute(select_sql, params)
        rows = cur2.fetchall()
    # Формируем CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    for row in rows:
        writer.writerow(row)
    content = buf.getvalue().encode("utf-8-sig")
    filename = f"{table}.csv"
    return filename, content


def logs_stats(days: int = 7) -> dict[str, Any]:
    """Сводные показатели за период."""
    stats: dict[str, Any] = {}
    with get_conn() as conn:
        # Действия по типам
        cur = conn.execute(
            """
            SELECT action, COUNT(*)
            FROM action_logs
            WHERE datetime(created_at) >= datetime('now', ?)
            GROUP BY action
            ORDER BY COUNT(*) DESC
            """,
            (f"-{int(days)} days",),
        )
        stats["actions_by_type"] = [(r[0], r[1]) for r in cur.fetchall()]
        # Антиспам итого
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM antispam_events
            WHERE datetime(created_at) >= datetime('now', ?)
            """,
            (f"-{int(days)} days",),
        )
        stats["antispam_total"] = cur.fetchone()[0]
        # Антиспам по типам
        cur = conn.execute(
            """
            SELECT kind, COUNT(*) FROM antispam_events
            WHERE datetime(created_at) >= datetime('now', ?)
            GROUP BY kind
            ORDER BY COUNT(*) DESC
            """,
            (f"-{int(days)} days",),
        )
        stats["antispam_by_kind"] = [(r[0], r[1]) for r in cur.fetchall()]
    return stats


def insert_promotion_request(
    requester_id: int,
    requester_username: str,
    requester_name: str,
    current_rank: str,
    target_rank: str,
    workbook_image_id: str,
    work_evidence_image_ids: str,
) -> int:
    """Создать заявку на повышение. Возвращает ID заявки."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO promotion_requests (
                requester_id, requester_username, requester_name,
                current_rank, target_rank, workbook_image_id, work_evidence_image_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (requester_id, requester_username, requester_name, 
             current_rank, target_rank, workbook_image_id, work_evidence_image_ids),
        )
        return cur.lastrowid


def get_promotion_request(request_id: int) -> Optional[Dict[str, Any]]:
    """Получить заявку на повышение по ID."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT id, requester_id, requester_username, requester_name,
                   current_rank, target_rank, workbook_image_id, work_evidence_image_ids,
                   status, moderator_id, moderator_username, moderator_rank,
                   reject_reason, decided_at, created_at
            FROM promotion_requests
            WHERE id = ?
            """,
            (request_id,),
        )
        row = cur.fetchone()
        if row:
            return {
                "id": row[0],
                "requester_id": row[1],
                "requester_username": row[2],
                "requester_name": row[3],
                "current_rank": row[4],
                "target_rank": row[5],
                "workbook_image_id": row[6],
                "work_evidence_image_ids": row[7],
                "status": row[8],
                "moderator_id": row[9],
                "moderator_username": row[10],
                "moderator_rank": row[11],
                "reject_reason": row[12],
                "decided_at": row[13],
                "created_at": row[14],
            }
        return None


def get_pending_promotion_requests() -> list:
    """Получить все заявки на повышение ожидающие модерации."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT id, requester_id, requester_username, requester_name,
                   current_rank, target_rank, created_at
            FROM promotion_requests
            WHERE status = 'pending'
            ORDER BY created_at ASC
            """,
        )
        return [
            {
                "id": row[0],
                "requester_id": row[1],
                "requester_username": row[2],
                "requester_name": row[3],
                "current_rank": row[4],
                "target_rank": row[5],
                "created_at": row[6],
            }
            for row in cur.fetchall()
        ]


def decide_promotion_request(
    request_id: int,
    moderator_id: int,
    moderator_username: str,
    moderator_rank: str,
    approved: bool,
    reject_reason: str = None,
) -> bool:
    """Принять решение по заявке на повышение."""
    status = "approved" if approved else "rejected"
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE promotion_requests
            SET status = ?, moderator_id = ?, moderator_username = ?, 
                moderator_rank = ?, reject_reason = ?, decided_at = datetime('now')
            WHERE id = ? AND status = 'pending'
            """,
            (status, moderator_id, moderator_username, moderator_rank, reject_reason, request_id),
        )
        return cur.rowcount > 0
