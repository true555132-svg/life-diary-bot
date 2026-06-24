"""Postgres 存取層 - 生活紀錄機器人"""
import os, threading
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL", "")
_lock = threading.Lock()


def _conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if not DATABASE_URL:
        print("[DB] DATABASE_URL 未設定，略過初始化")
        return
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS diary_entries (
                id          SERIAL PRIMARY KEY,
                user_id     TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                entry_type  TEXT NOT NULL DEFAULT 'text',
                content     TEXT DEFAULT '',
                image_url   TEXT DEFAULT ''
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_diary_user_time ON diary_entries(user_id, created_at)")
        conn.commit()
        cur.close()
        conn.close()


def insert_entry(user_id: str, entry_type: str, content: str = "", image_url: str = ""):
    if not DATABASE_URL:
        return
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO diary_entries (user_id, entry_type, content, image_url) VALUES (%s,%s,%s,%s)",
            (user_id, entry_type, content, image_url),
        )
        conn.commit()
        cur.close()
        conn.close()


def get_entries(user_id: str, since_days: int):
    if not DATABASE_URL:
        return []
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT created_at, entry_type, content, image_url
        FROM diary_entries
        WHERE user_id=%s AND created_at >= now() - interval '%s days'
        ORDER BY created_at ASC
        """,
        (user_id, since_days),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"created_at": r[0].isoformat(), "entry_type": r[1], "content": r[2] or "", "image_url": r[3] or ""}
        for r in rows
    ]


def get_distinct_user_ids():
    if not DATABASE_URL:
        return []
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM diary_entries")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]
