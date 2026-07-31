"""Postgres (Supabase) での永続化。

ユーザー・確認コード・サブスクリプション・プロフィール・科目・ノート・利用履歴。

接続先は環境変数 DATABASE_URL。Supabaseダッシュボードの
Project Settings → Database → Connection string から取得する。
接続数が限られるので、コネクションプールを使い回す。
"""
import os
import random
from contextlib import contextmanager
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from plans import PLANS

CODE_TTL_MIN = 10
CYCLE_DAYS = 30
# 列名の一部として使うため、必ずこのリストで検証してから埋め込む
PROVIDERS = ("claude", "openai", "deepseek")

_pool = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = os.getenv("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL が設定されていません。Supabase の Connection string を "
                "環境変数 DATABASE_URL に設定してください。")
        _pool = ConnectionPool(url, min_size=1, max_size=5,
                               kwargs={"row_factory": dict_row}, open=True)
    return _pool


@contextmanager
def _conn():
    """プールから接続を借りる。正常終了でcommit、例外でrollback、必ずプールに返す。"""
    with _get_pool().connection() as conn:
        yield conn


def _check_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError(f"不明なプロバイダ: {provider}")
    return provider


def init_db():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            verified INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            code TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            plan TEXT NOT NULL,
            renews_at TEXT NOT NULL,
            remaining_claude INTEGER NOT NULL,
            remaining_openai INTEGER NOT NULL,
            remaining_deepseek INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            provider TEXT NOT NULL,
            tokens_in INTEGER NOT NULL,
            tokens_out INTEGER NOT NULL,
            task TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            preferred_name TEXT NOT NULL,
            major TEXT,
            school TEXT
        );
        CREATE TABLE IF NOT EXISTS courses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (user_id, name)
        );
        CREATE TABLE IF NOT EXISTS notes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            course_id INTEGER REFERENCES courses(id),
            kind TEXT NOT NULL,
            filename TEXT NOT NULL,
            source_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS canvas_accounts (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            base_url TEXT NOT NULL,
            encrypted_token TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS extension_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            token_hash TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        );
        """)


# ---------------------------------------------------------------- users
def create_user(email: str, username: str) -> int:
    with _conn() as c:
        row = c.execute(
            "INSERT INTO users (email, username, created_at) "
            "VALUES (%s, %s, %s) RETURNING id",
            (email, username, datetime.utcnow().isoformat())).fetchone()
        return row["id"]


def get_user_by_email(email: str):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE email = %s",
                         (email,)).fetchone()


def get_user(user_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE id = %s",
                         (user_id,)).fetchone()


def mark_verified(user_id: int):
    with _conn() as c:
        c.execute("UPDATE users SET verified = 1 WHERE id = %s", (user_id,))


# ------------------------------------------------------------- verification
def issue_code(user_id: int, purpose: str) -> str:
    code = f"{random.randint(0, 999999):06d}"
    expires = (datetime.utcnow() + timedelta(minutes=CODE_TTL_MIN)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO codes (user_id, code, purpose, expires_at, used) "
            "VALUES (%s, %s, %s, %s, 0)", (user_id, code, purpose, expires))
    return code


def check_code(user_id: int, purpose: str, code: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM codes WHERE user_id=%s AND purpose=%s AND code=%s "
            "AND used=0 ORDER BY id DESC LIMIT 1",
            (user_id, purpose, code)).fetchone()
        if not row or row["expires_at"] < datetime.utcnow().isoformat():
            return False
        c.execute("UPDATE codes SET used=1 WHERE id=%s", (row["id"],))
        return True


# ------------------------------------------------------------ subscriptions
def _fresh_allowance(plan: str) -> dict:
    return dict(PLANS[plan]["monthly_tokens"])


def set_plan(user_id: int, plan: str):
    """プラン購入(モック決済)。トークン残量をそのプランの月間付与量にリセット。"""
    allowance = _fresh_allowance(plan)
    renews_at = (datetime.utcnow() + timedelta(days=CYCLE_DAYS)).isoformat()
    with _conn() as c:
        c.execute("""
            INSERT INTO subscriptions
                (user_id, plan, renews_at, remaining_claude, remaining_openai,
                 remaining_deepseek)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                plan=EXCLUDED.plan, renews_at=EXCLUDED.renews_at,
                remaining_claude=EXCLUDED.remaining_claude,
                remaining_openai=EXCLUDED.remaining_openai,
                remaining_deepseek=EXCLUDED.remaining_deepseek
        """, (user_id, plan, renews_at, allowance["claude"],
              allowance["openai"], allowance["deepseek"]))


def get_subscription(user_id: int):
    """現在のサブスクリプション。周期が切れていれば自動更新してから返す。"""
    with _conn() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE user_id=%s",
                        (user_id,)).fetchone()
        if row and row["renews_at"] < datetime.utcnow().isoformat():
            allowance = _fresh_allowance(row["plan"])
            renews_at = (datetime.utcnow() + timedelta(days=CYCLE_DAYS)).isoformat()
            c.execute("""UPDATE subscriptions SET renews_at=%s, remaining_claude=%s,
                         remaining_openai=%s, remaining_deepseek=%s
                         WHERE user_id=%s""",
                      (renews_at, allowance["claude"], allowance["openai"],
                       allowance["deepseek"], user_id))
            row = c.execute("SELECT * FROM subscriptions WHERE user_id=%s",
                            (user_id,)).fetchone()
        return row


def has_tokens(user_id: int, provider: str, need: int = 1) -> bool:
    _check_provider(provider)
    sub = get_subscription(user_id)
    if not sub:
        return False
    return sub[f"remaining_{provider}"] >= need


def deduct_tokens(user_id: int, provider: str, tokens_in: int, tokens_out: int,
                  task: str):
    _check_provider(provider)
    total = tokens_in + tokens_out
    with _conn() as c:
        c.execute(f"""UPDATE subscriptions SET remaining_{provider} =
                     GREATEST(0, remaining_{provider} - %s) WHERE user_id=%s""",
                  (total, user_id))
        c.execute("""INSERT INTO usage_log (user_id, provider, tokens_in,
                     tokens_out, task, created_at)
                     VALUES (%s, %s, %s, %s, %s, %s)""",
                  (user_id, provider, tokens_in, tokens_out, task,
                   datetime.utcnow().isoformat()))


def usage_this_cycle(user_id: int) -> list:
    sub = get_subscription(user_id)
    if not sub:
        return []
    cycle_start = (datetime.fromisoformat(sub["renews_at"])
                   - timedelta(days=CYCLE_DAYS)).isoformat()
    with _conn() as c:
        return c.execute(
            "SELECT * FROM usage_log WHERE user_id=%s AND created_at>=%s "
            "ORDER BY id DESC", (user_id, cycle_start)).fetchall()


# --------------------------------------------------------------- profile
def set_profile(user_id: int, preferred_name: str, major: str, school: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO profiles (user_id, preferred_name, major, school)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                preferred_name=EXCLUDED.preferred_name,
                major=EXCLUDED.major, school=EXCLUDED.school
        """, (user_id, preferred_name, major, school))


def get_profile(user_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM profiles WHERE user_id=%s",
                         (user_id,)).fetchone()


# --------------------------------------------------------------- courses
def add_course(user_id: int, name: str) -> int:
    """科目を追加(既にあればそのidを返す)。"""
    name = name.strip()
    with _conn() as c:
        row = c.execute(
            "INSERT INTO courses (user_id, name, created_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, name) DO UPDATE SET name=EXCLUDED.name "
            "RETURNING id",
            (user_id, name, datetime.utcnow().isoformat())).fetchone()
        return row["id"]


def list_user_courses(user_id: int) -> list:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM courses WHERE user_id=%s ORDER BY name",
            (user_id,)).fetchall()


def get_course(user_id: int, course_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM courses WHERE id=%s AND user_id=%s",
                         (course_id, user_id)).fetchone()


# ------------------------------------------------------------------ notes
def add_note(user_id: int, course_id: int | None, kind: str, filename: str,
             source_name: str):
    with _conn() as c:
        c.execute("""INSERT INTO notes
                     (user_id, course_id, kind, filename, source_name, created_at)
                     VALUES (%s, %s, %s, %s, %s, %s)""",
                  (user_id, course_id, kind, filename, source_name,
                   datetime.utcnow().isoformat()))


def notes_count_by_course(user_id: int) -> dict:
    """course_id -> ノート件数(未分類はNoneキー)。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT course_id, COUNT(*) AS n FROM notes WHERE user_id=%s "
            "GROUP BY course_id", (user_id,)).fetchall()
        return {r["course_id"]: r["n"] for r in rows}


def list_notes_for_course(user_id: int, course_id: int | None) -> list:
    with _conn() as c:
        if course_id is None:
            return c.execute(
                "SELECT * FROM notes WHERE user_id=%s AND course_id IS NULL "
                "ORDER BY created_at DESC", (user_id,)).fetchall()
        return c.execute(
            "SELECT * FROM notes WHERE user_id=%s AND course_id=%s "
            "ORDER BY created_at DESC", (user_id, course_id)).fetchall()


def get_note(user_id: int, note_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM notes WHERE id=%s AND user_id=%s",
                         (note_id, user_id)).fetchone()


# --------------------------------------------------------------- canvas
def set_canvas_account(user_id: int, base_url: str, encrypted_token: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO canvas_accounts (user_id, base_url, encrypted_token, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                base_url=EXCLUDED.base_url,
                encrypted_token=EXCLUDED.encrypted_token
        """, (user_id, base_url, encrypted_token, datetime.utcnow().isoformat()))


def get_canvas_account(user_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM canvas_accounts WHERE user_id=%s",
                         (user_id,)).fetchone()


def delete_canvas_account(user_id: int):
    with _conn() as c:
        c.execute("DELETE FROM canvas_accounts WHERE user_id=%s", (user_id,))


# ----------------------------------------------------------- extension token
def create_extension_token(user_id: int, token_hash: str):
    """既存のトークンを失効させ、新しい1つだけを有効にする(1ユーザー1トークン)。"""
    with _conn() as c:
        c.execute("DELETE FROM extension_tokens WHERE user_id=%s", (user_id,))
        c.execute("""INSERT INTO extension_tokens (user_id, token_hash, created_at)
                     VALUES (%s, %s, %s)""",
                  (user_id, token_hash, datetime.utcnow().isoformat()))


def get_user_by_extension_token_hash(token_hash: str):
    with _conn() as c:
        row = c.execute(
            "SELECT user_id FROM extension_tokens WHERE token_hash=%s",
            (token_hash,)).fetchone()
        if row:
            c.execute("UPDATE extension_tokens SET last_used_at=%s WHERE token_hash=%s",
                     (datetime.utcnow().isoformat(), token_hash))
        return row["user_id"] if row else None