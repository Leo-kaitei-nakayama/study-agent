"""SQLiteでの永続化。ユーザー・確認コード・サブスクリプション・利用履歴。"""
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from plans import PLANS

DB_PATH = Path(__file__).parent / "instance" / "study_agent.db"
CODE_TTL_MIN = 10
CYCLE_DAYS = 30


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            verified INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            provider TEXT NOT NULL,
            tokens_in INTEGER NOT NULL,
            tokens_out INTEGER NOT NULL,
            task TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


# ---------------------------------------------------------------- users
def create_user(email: str, username: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO users (email, username, created_at) VALUES (?, ?, ?)",
            (email, username, datetime.utcnow().isoformat()))
        return cur.lastrowid


def get_user_by_email(email: str):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def get_user(user_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def mark_verified(user_id: int):
    with _conn() as c:
        c.execute("UPDATE users SET verified = 1 WHERE id = ?", (user_id,))


# ------------------------------------------------------------- verification
def issue_code(user_id: int, purpose: str) -> str:
    code = f"{random.randint(0, 999999):06d}"
    expires = (datetime.utcnow() + timedelta(minutes=CODE_TTL_MIN)).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO codes (user_id, code, purpose, expires_at, used) "
            "VALUES (?, ?, ?, ?, 0)", (user_id, code, purpose, expires))
    return code


def check_code(user_id: int, purpose: str, code: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM codes WHERE user_id=? AND purpose=? AND code=? "
            "AND used=0 ORDER BY id DESC LIMIT 1",
            (user_id, purpose, code)).fetchone()
        if not row or row["expires_at"] < datetime.utcnow().isoformat():
            return False
        c.execute("UPDATE codes SET used=1 WHERE id=?", (row["id"],))
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
                (user_id, plan, renews_at, remaining_claude, remaining_openai, remaining_deepseek)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                plan=excluded.plan, renews_at=excluded.renews_at,
                remaining_claude=excluded.remaining_claude,
                remaining_openai=excluded.remaining_openai,
                remaining_deepseek=excluded.remaining_deepseek
        """, (user_id, plan, renews_at, allowance["claude"],
              allowance["openai"], allowance["deepseek"]))


def get_subscription(user_id: int):
    """現在のサブスクリプション。周期が切れていれば自動更新してから返す。"""
    with _conn() as c:
        row = c.execute("SELECT * FROM subscriptions WHERE user_id=?",
                        (user_id,)).fetchone()
        if row and row["renews_at"] < datetime.utcnow().isoformat():
            allowance = _fresh_allowance(row["plan"])
            renews_at = (datetime.utcnow() + timedelta(days=CYCLE_DAYS)).isoformat()
            c.execute("""UPDATE subscriptions SET renews_at=?, remaining_claude=?,
                         remaining_openai=?, remaining_deepseek=? WHERE user_id=?""",
                      (renews_at, allowance["claude"], allowance["openai"],
                       allowance["deepseek"], user_id))
            row = c.execute("SELECT * FROM subscriptions WHERE user_id=?",
                            (user_id,)).fetchone()
        return row


def has_tokens(user_id: int, provider: str, need: int = 1) -> bool:
    sub = get_subscription(user_id)
    if not sub:
        return False
    return sub[f"remaining_{provider}"] >= need


def deduct_tokens(user_id: int, provider: str, tokens_in: int, tokens_out: int,
                  task: str):
    total = tokens_in + tokens_out
    with _conn() as c:
        c.execute(f"""UPDATE subscriptions SET remaining_{provider} =
                     MAX(0, remaining_{provider} - ?) WHERE user_id=?""",
                  (total, user_id))
        c.execute("""INSERT INTO usage (user_id, provider, tokens_in, tokens_out,
                     task, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
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
            "SELECT * FROM usage WHERE user_id=? AND created_at>=? ORDER BY id DESC",
            (user_id, cycle_start)).fetchall()
