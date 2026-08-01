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

from plans import LEGACY_TOKEN_ALLOWANCE

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
        -- 表示言語。既存の DB にも後から足せるように ALTER で追加する
        -- (CREATE TABLE IF NOT EXISTS は既存テーブルに列を足してくれないため)。
        ALTER TABLE profiles ADD COLUMN IF NOT EXISTS lang TEXT;
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
        -- 表示名 `Week {N}: {課題名}` と、その内訳。
        -- week/term は絞り込み(クォーター別タブ・今週の課題)に使うので列に持つ。
        ALTER TABLE notes ADD COLUMN IF NOT EXISTS title TEXT;
        ALTER TABLE notes ADD COLUMN IF NOT EXISTS week_no INTEGER;
        ALTER TABLE notes ADD COLUMN IF NOT EXISTS term TEXT;
        -- Canvas から取り込んだ課題。締切と「今週やること」の判定に使う。
        -- 同じ課題を毎回の同期で重複させないよう (user, course, name) で一意。
        CREATE TABLE IF NOT EXISTS assignments (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            course_id INTEGER REFERENCES courses(id),
            name TEXT NOT NULL,
            due_at TEXT,
            points NUMERIC(7, 2),
            url TEXT,
            description TEXT,
            week_no INTEGER,
            term TEXT,
            kind TEXT NOT NULL DEFAULT 'other',
            status TEXT NOT NULL DEFAULT 'todo',
            note_id INTEGER REFERENCES notes(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (user_id, course_id, name)
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
        CREATE TABLE IF NOT EXISTS external_links (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            course_id INTEGER REFERENCES courses(id),
            url TEXT NOT NULL,
            label TEXT,
            is_thin_syllabus INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            captured_at TEXT,
            UNIQUE (user_id, url)
        );
        -- クレジット残高(USD)。画面にはトークン数ではなくこれを出す。
        CREATE TABLE IF NOT EXISTS credits (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            balance_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        -- チャージと利用の明細。delta_usd は チャージ=+, 利用=- 。
        CREATE TABLE IF NOT EXISTS credit_ledger (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            delta_usd NUMERIC(12, 6) NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        -- 授業で使うサイトのログイン情報。password は crypto.encrypt() 済みの文字列。
        CREATE TABLE IF NOT EXISTS site_credentials (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            course_id INTEGER REFERENCES courses(id),
            label TEXT NOT NULL,
            site_url TEXT NOT NULL,
            username TEXT NOT NULL,
            encrypted_password TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            UNIQUE (user_id, site_url)
        );
        -- 成績表アップロードの記録(1ユーザー1件、上書き)。
        CREATE TABLE IF NOT EXISTS transcripts (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            uploaded_at TEXT NOT NULL,
            source_name TEXT NOT NULL
        );
        -- 大学が計算済みの集計値(成績表末尾の UC GPA など)。
        -- 編入単位は履修行として載らないため、自前の合計より公式値の方が正しい。
        -- 読み取れなかった場合は NULL で、そのときは履修行から計算する。
        ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS official_gpa NUMERIC(5, 3);
        ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS official_gpa_units NUMERIC(7, 2);
        ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS official_grade_points NUMERIC(9, 2);
        ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS official_units_completed NUMERIC(7, 2);
        -- 成績表から取り出した履修1件ずつ。再アップロード時は総入れ替え。
        CREATE TABLE IF NOT EXISTS transcript_courses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            term TEXT NOT NULL,
            code TEXT NOT NULL,
            title TEXT,
            units NUMERIC(5, 2) NOT NULL,
            grade TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'upload'
        );
        """)
    # 後から足した列(term など)を既存行にも埋める
    backfill_note_terms()


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
def backfill_note_terms():
    """term / week_no / title が空のノートを、作成日から埋める。

    これらの列は後から足したので、既存のノートは NULL のまま。放っておくと
    クォーター別タブの絞り込みに引っかからず画面から消えてしまうため、
    起動時に一度だけ埋める(既に入っている行は触らない)。
    """
    import weeks

    with _conn() as c:
        rows = c.execute(
            "SELECT id, source_name, created_at FROM notes WHERE term IS NULL"
        ).fetchall()
        for r in rows:
            wk = weeks.week_of(r["source_name"], r["created_at"])
            c.execute(
                "UPDATE notes SET term=%s, week_no=COALESCE(week_no, %s), "
                "title=COALESCE(title, %s) WHERE id=%s",
                (weeks.term_of(r["created_at"]), wk,
                 weeks.note_title(r["source_name"], r["created_at"], wk), r["id"]))
        return len(rows)


def _fresh_allowance(plan: str) -> dict:
    """旧トークン方式の付与量。クレジット方式に移行したので常に 0。

    subscriptions テーブルの NOT NULL 制約を満たすためだけに残っている。
    利用可否は has_credit()(= クレジット残高)で判定する。
    """
    return dict(LEGACY_TOKEN_ALLOWANCE)


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
    """旧インターフェース。クレジット方式に移行したので残高だけを見る。

    プロバイダごとの上限は無くなったので provider 引数は検証だけして無視する。
    """
    _check_provider(provider)
    return has_credit(user_id)


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


def set_lang(user_id: int, lang: str):
    """表示言語をプロフィールに覚えさせる(端末を変えても引き継ぐため)。

    プロフィール作成前に言語を切り替えることもあるので、行が無ければ何もしない
    (その場合はセッションにだけ残り、オンボーディング時に保存される)。
    """
    with _conn() as c:
        c.execute("UPDATE profiles SET lang=%s WHERE user_id=%s", (lang, user_id))


def get_lang(user_id: int) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT lang FROM profiles WHERE user_id=%s",
                        (user_id,)).fetchone()
        return row["lang"] if row else None


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
             source_name: str, title: str | None = None,
             week_no: int | None = None, term: str | None = None) -> int:
    """ノートを 1 件記録して id を返す。

    title は `Week {N}: {課題名}` の形の表示名(weeks.note_title が作る)。
    week_no / term はクォーター別タブと「今週」の絞り込みに使う。
    """
    with _conn() as c:
        row = c.execute("""INSERT INTO notes
                     (user_id, course_id, kind, filename, source_name,
                      title, week_no, term, created_at)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                     RETURNING id""",
                  (user_id, course_id, kind, filename, source_name,
                   title, week_no, term, datetime.utcnow().isoformat())).fetchone()
        return row["id"]


def notes_count_by_course(user_id: int, term: str | None = None) -> dict:
    """course_id -> ノート件数(未分類はNoneキー)。term を渡すとその学期だけ。"""
    with _conn() as c:
        if term:
            rows = c.execute(
                "SELECT course_id, COUNT(*) AS n FROM notes "
                "WHERE user_id=%s AND term=%s GROUP BY course_id",
                (user_id, term)).fetchall()
        else:
            rows = c.execute(
                "SELECT course_id, COUNT(*) AS n FROM notes WHERE user_id=%s "
                "GROUP BY course_id", (user_id,)).fetchall()
        return {r["course_id"]: r["n"] for r in rows}


def list_note_terms(user_id: int) -> list[str]:
    """ノートが存在する学期の一覧(タブに出す)。term 未設定のものは除く。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT term FROM notes WHERE user_id=%s AND term IS NOT NULL",
            (user_id,)).fetchall()
        return [r["term"] for r in rows]


def list_notes_for_course(user_id: int, course_id: int | None,
                          term: str | None = None) -> list:
    """1 科目のノート一覧。週の新しい順 → 作成の新しい順。"""
    where = ["user_id=%s"]
    params: list = [user_id]
    if course_id is None:
        where.append("course_id IS NULL")
    else:
        where.append("course_id=%s")
        params.append(course_id)
    if term:
        where.append("term=%s")
        params.append(term)
    with _conn() as c:
        return c.execute(
            f"SELECT * FROM notes WHERE {' AND '.join(where)} "
            "ORDER BY week_no DESC NULLS LAST, created_at DESC",
            tuple(params)).fetchall()


def get_note(user_id: int, note_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM notes WHERE id=%s AND user_id=%s",
                         (note_id, user_id)).fetchone()


def list_notes_for_term(user_id: int, term: str | None = None) -> list:
    """その学期のノートを科目名つきで全部返す(削除の選択画面に出す)。"""
    where = ["n.user_id=%s"]
    params: list = [user_id]
    if term:
        where.append("n.term=%s")
        params.append(term)
    with _conn() as c:
        return c.execute(f"""
            SELECT n.*, c.name AS course_name FROM notes n
            LEFT JOIN courses c ON c.id = n.course_id
            WHERE {' AND '.join(where)}
            ORDER BY c.name NULLS FIRST, n.week_no DESC NULLS LAST, n.created_at DESC
        """, tuple(params)).fetchall()


def get_notes_by_ids(user_id: int, note_ids: list[int]) -> list:
    """選ばれた id のノートを返す(確認画面に「何を消すか」を出すため)。"""
    if not note_ids:
        return []
    with _conn() as c:
        return c.execute("""
            SELECT n.*, c.name AS course_name FROM notes n
            LEFT JOIN courses c ON c.id = n.course_id
            WHERE n.user_id=%s AND n.id = ANY(%s)
            ORDER BY c.name NULLS FIRST, n.created_at DESC
        """, (user_id, list(note_ids))).fetchall()


def delete_notes(user_id: int, note_ids: list[int]) -> list[str]:
    """選んだノートを削除し、消したファイル名を返す(実ファイルの削除は呼び出し側)。"""
    if not note_ids:
        return []
    with _conn() as c:
        rows = c.execute(
            "DELETE FROM notes WHERE user_id=%s AND id = ANY(%s) RETURNING filename",
            (user_id, list(note_ids))).fetchall()
        return [r["filename"] for r in rows]


def delete_all_notes(user_id: int, course_id: int | None = None,
                     term: str | None = None) -> list[str]:
    """まとめて削除。course_id / term を渡すとその範囲だけ。

    course_id を渡さない場合は「未分類も含めた全部」が対象になる。
    """
    where = ["user_id=%s"]
    params: list = [user_id]
    if course_id is not None:
        where.append("course_id=%s")
        params.append(course_id)
    if term:
        where.append("term=%s")
        params.append(term)
    with _conn() as c:
        rows = c.execute(
            f"DELETE FROM notes WHERE {' AND '.join(where)} RETURNING filename",
            tuple(params)).fetchall()
        return [r["filename"] for r in rows]


# ------------------------------------------------------------- assignments
def upsert_assignment(user_id: int, course_id: int | None, name: str,
                      due_at: str | None = None, points=None, url: str | None = None,
                      description: str | None = None, week_no: int | None = None,
                      term: str | None = None, kind: str = "other") -> int:
    """Canvas から取り込んだ課題を 1 件登録(同じ名前があれば更新)。

    status と note_id は上書きしない。エージェントが下書きを作った実績を
    同期のたびに消してしまわないため。
    """
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute("""
            INSERT INTO assignments (user_id, course_id, name, due_at, points, url,
                                     description, week_no, term, kind,
                                     created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, course_id, name) DO UPDATE SET
                due_at=EXCLUDED.due_at, points=EXCLUDED.points, url=EXCLUDED.url,
                description=EXCLUDED.description, week_no=EXCLUDED.week_no,
                term=EXCLUDED.term, kind=EXCLUDED.kind, updated_at=EXCLUDED.updated_at
            RETURNING id
        """, (user_id, course_id, name, due_at, points, url, description,
              week_no, term, kind, now, now)).fetchone()
        return row["id"]


def list_assignments(user_id: int, term: str | None = None,
                     week_no: int | None = None, course_id: int | None = None) -> list:
    """課題一覧。締切の早い順(締切なしは最後)。"""
    where = ["a.user_id=%s"]
    params: list = [user_id]
    for col, val in (("a.term", term), ("a.week_no", week_no),
                     ("a.course_id", course_id)):
        if val is not None:
            where.append(f"{col}=%s")
            params.append(val)
    with _conn() as c:
        rows = c.execute(f"""
            SELECT a.*, c.name AS course_name FROM assignments a
            LEFT JOIN courses c ON c.id = a.course_id
            WHERE {' AND '.join(where)}
            ORDER BY a.due_at ASC NULLS LAST, a.name
        """, tuple(params)).fetchall()
        for r in rows:
            if r["points"] is not None:
                r["points"] = float(r["points"])
        return rows


def get_assignments_by_ids(user_id: int, ids: list[int]) -> list:
    """選ばれた課題を返す(下書きするものをユーザーが選んだとき)。

    他人の課題は user_id の条件で落ちるので、id を直接渡されても漏れない。
    """
    if not ids:
        return []
    with _conn() as c:
        rows = c.execute("""
            SELECT a.*, c.name AS course_name FROM assignments a
            LEFT JOIN courses c ON c.id = a.course_id
            WHERE a.user_id=%s AND a.id = ANY(%s)
            ORDER BY a.due_at ASC NULLS LAST, a.name
        """, (user_id, list(ids))).fetchall()
        for r in rows:
            if r["points"] is not None:
                r["points"] = float(r["points"])
        return rows


def get_assignment(user_id: int, assignment_id: int):
    with _conn() as c:
        return c.execute("""
            SELECT a.*, c.name AS course_name FROM assignments a
            LEFT JOIN courses c ON c.id = a.course_id
            WHERE a.id=%s AND a.user_id=%s""",
            (assignment_id, user_id)).fetchone()


def set_assignment_status(user_id: int, assignment_id: int, status: str,
                          note_id: int | None = None):
    """課題の状態を更新する。status は todo / drafted / reviewed。

    **提出は行わない。** drafted は「下書きができた」という意味でしかなく、
    提出するかどうかは必ず学生が決める。
    """
    with _conn() as c:
        c.execute("""UPDATE assignments SET status=%s, updated_at=%s,
                     note_id=COALESCE(%s, note_id)
                     WHERE id=%s AND user_id=%s""",
                  (status, datetime.utcnow().isoformat(), note_id,
                   assignment_id, user_id))


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


# --------------------------------------------------- external links (shim)
def add_external_link(user_id: int, course_id: int | None, url: str,
                      label: str, is_thin: bool):
    """シラバス内で見つかった外部リンクを記録。既にあれば情報だけ更新する。"""
    with _conn() as c:
        c.execute("""
            INSERT INTO external_links
                (user_id, course_id, url, label, is_thin_syllabus, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, url) DO UPDATE SET
                course_id = COALESCE(EXCLUDED.course_id, external_links.course_id),
                label = EXCLUDED.label,
                is_thin_syllabus = EXCLUDED.is_thin_syllabus
        """, (user_id, course_id, url, label, 1 if is_thin else 0,
              datetime.utcnow().isoformat()))


def list_pending_links(user_id: int, thin_only: bool = False) -> list:
    """まだ取り込まれていない外部リンク。thin_only=Trueなら
    「シラバス本体が外部にある」と判定されたものだけ返す。"""
    sql = ("SELECT el.*, c.name AS course_name FROM external_links el "
           "LEFT JOIN courses c ON c.id = el.course_id "
           "WHERE el.user_id=%s AND el.captured_at IS NULL")
    if thin_only:
        sql += " AND el.is_thin_syllabus = 1"
    sql += " ORDER BY el.is_thin_syllabus DESC, el.id"
    with _conn() as c:
        return c.execute(sql, (user_id,)).fetchall()


def mark_link_captured(user_id: int, url: str) -> bool:
    """そのURLが未取り込みリンクとして登録されていれば、取り込み済みにする。"""
    with _conn() as c:
        cur = c.execute(
            "UPDATE external_links SET captured_at=%s "
            "WHERE user_id=%s AND url=%s AND captured_at IS NULL",
            (datetime.utcnow().isoformat(), user_id, url))
        return cur.rowcount > 0


# ------------------------------------------------------------------ credits
# 画面に出すのは「トークン残量」ではなく「USD のクレジット残高」。
# AI を呼ぶたびに概算コストを引き、残高が 0 以下なら実行を止める。
def get_balance(user_id: int) -> float:
    with _conn() as c:
        row = c.execute("SELECT balance_usd FROM credits WHERE user_id=%s",
                        (user_id,)).fetchone()
        return float(row["balance_usd"]) if row else 0.0


def add_credit(user_id: int, amount_usd: float, reason: str) -> float:
    """チャージ(正の数)。新しい残高を返す。"""
    return _move_credit(user_id, abs(float(amount_usd)), reason)


def charge_credit(user_id: int, amount_usd: float, reason: str) -> float:
    """利用分を差し引く(負の数として記録)。新しい残高を返す。

    残高はマイナスを許す。1回の呼び出しの途中で尽きても処理は完了させ、
    次の実行を has_credit() で止める方が、生成物が中途半端にならない。
    """
    return _move_credit(user_id, -abs(float(amount_usd)), reason)


def _move_credit(user_id: int, delta_usd: float, reason: str) -> float:
    now = datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute("""
            INSERT INTO credits (user_id, balance_usd, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                balance_usd = credits.balance_usd + EXCLUDED.balance_usd,
                updated_at = EXCLUDED.updated_at
            RETURNING balance_usd
        """, (user_id, delta_usd, now)).fetchone()
        c.execute("""INSERT INTO credit_ledger (user_id, delta_usd, reason, created_at)
                     VALUES (%s, %s, %s, %s)""", (user_id, delta_usd, reason, now))
        return float(row["balance_usd"])


def has_credit(user_id: int) -> bool:
    """残高が残っているか。AI タスクを始める前のチェックに使う。"""
    return get_balance(user_id) > 0


def list_credit_ledger(user_id: int, limit: int = 20) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM credit_ledger WHERE user_id=%s "
            "ORDER BY id DESC LIMIT %s", (user_id, limit)).fetchall()
        for r in rows:
            r["delta_usd"] = float(r["delta_usd"])
        return rows


# ------------------------------------------------------- site credentials
def add_site_credential(user_id: int, label: str, site_url: str, username: str,
                        encrypted_password: str, course_id: int | None = None):
    """サイトのログイン情報を保存(同じ URL があれば上書き)。

    encrypted_password は crypto.encrypt() を通した文字列であること。
    このモジュールは暗号化の中身を知らない(平文を受け取らない)。
    """
    with _conn() as c:
        c.execute("""
            INSERT INTO site_credentials
                (user_id, course_id, label, site_url, username,
                 encrypted_password, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, site_url) DO UPDATE SET
                label=EXCLUDED.label, username=EXCLUDED.username,
                encrypted_password=EXCLUDED.encrypted_password,
                course_id=COALESCE(EXCLUDED.course_id, site_credentials.course_id)
        """, (user_id, course_id, label, site_url, username, encrypted_password,
              datetime.utcnow().isoformat()))


def list_site_credentials(user_id: int) -> list:
    """一覧表示用。暗号化済みパスワードも含むので、画面には出さないこと。"""
    with _conn() as c:
        return c.execute("""
            SELECT sc.*, c.name AS course_name FROM site_credentials sc
            LEFT JOIN courses c ON c.id = sc.course_id
            WHERE sc.user_id=%s ORDER BY sc.label
        """, (user_id,)).fetchall()


def get_site_credential(user_id: int, site_url: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM site_credentials WHERE user_id=%s AND site_url=%s",
            (user_id, site_url)).fetchone()


def delete_site_credential(user_id: int, cred_id: int):
    with _conn() as c:
        c.execute("DELETE FROM site_credentials WHERE id=%s AND user_id=%s",
                  (cred_id, user_id))


def touch_site_credential(user_id: int, site_url: str):
    """エージェントがそのログイン情報を使ったときに最終使用日時を更新する。"""
    with _conn() as c:
        c.execute("UPDATE site_credentials SET last_used_at=%s "
                  "WHERE user_id=%s AND site_url=%s",
                  (datetime.utcnow().isoformat(), user_id, site_url))


# --------------------------------------------------------------- transcript
def replace_transcript(user_id: int, source_name: str, courses: list[dict],
                       official: dict | None = None):
    """成績表を丸ごと入れ替える(アップロードのたびに総入れ替え)。

    courses の各要素は {"term", "code", "title", "units", "grade"} を持つこと。
    official は大学が計算済みの集計値(transcript.parse_transcript_html が返す)。
    GPA はここでは計算しない(transcript.py の役目)。
    """
    now = datetime.utcnow().isoformat()
    official = official or {}
    with _conn() as c:
        c.execute("DELETE FROM transcript_courses WHERE user_id=%s AND source='upload'",
                  (user_id,))
        for row in courses:
            c.execute("""INSERT INTO transcript_courses
                         (user_id, term, code, title, units, grade, source)
                         VALUES (%s, %s, %s, %s, %s, %s, 'upload')""",
                      (user_id, row.get("term", "Unknown"), row["code"],
                       row.get("title", ""), row["units"], row["grade"]))
        c.execute("""
            INSERT INTO transcripts (user_id, uploaded_at, source_name,
                                     official_gpa, official_gpa_units,
                                     official_grade_points, official_units_completed)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                uploaded_at=EXCLUDED.uploaded_at,
                source_name=EXCLUDED.source_name,
                official_gpa=EXCLUDED.official_gpa,
                official_gpa_units=EXCLUDED.official_gpa_units,
                official_grade_points=EXCLUDED.official_grade_points,
                official_units_completed=EXCLUDED.official_units_completed
        """, (user_id, now, source_name, official.get("gpa"),
              official.get("gpa_units"), official.get("grade_points"),
              official.get("units_completed")))


def get_official_totals(user_id: int) -> dict:
    """保存済みの公式集計値を transcript.compute_gpa() に渡せる形で返す。

    値が無い(読み取れなかった)項目は入れないので、呼び出し側では
    「あるものだけ公式値を使う」動きになる。
    """
    row = get_transcript_meta(user_id)
    if not row:
        return {}
    mapping = {"gpa": "official_gpa", "gpa_units": "official_gpa_units",
               "grade_points": "official_grade_points",
               "units_completed": "official_units_completed"}
    return {key: float(row[col]) for key, col in mapping.items()
            if row.get(col) is not None}


def add_transcript_course(user_id: int, term: str, code: str, title: str,
                          units: float, grade: str, source: str = "manual"):
    """履修を 1 件足す。

    source='manual'(既定)なら、後で成績表を再アップロードしても消えない。
    """
    with _conn() as c:
        c.execute("""INSERT INTO transcript_courses
                     (user_id, term, code, title, units, grade, source)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                  (user_id, term, code, title, units, grade, source))


def set_transcript_grade(user_id: int, row_id: int, grade: str) -> bool:
    """履修中の科目に、後から出た成績を入れる。

    同時に source を 'manual' にする。こうしておくと、次に履修予定表や
    成績表を取り込み直しても、手で入れた成績が上書きされない。
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE transcript_courses SET grade=%s, source='manual' "
            "WHERE id=%s AND user_id=%s", (grade, row_id, user_id))
        return cur.rowcount > 0


def list_transcript_courses(user_id: int) -> list:
    """GPA 計算に渡せる形(units は float)で履修一覧を返す。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM transcript_courses WHERE user_id=%s ORDER BY id",
            (user_id,)).fetchall()
        for r in rows:
            r["units"] = float(r["units"])
        return rows


def delete_transcript_course(user_id: int, row_id: int):
    with _conn() as c:
        c.execute("DELETE FROM transcript_courses WHERE id=%s AND user_id=%s",
                  (row_id, user_id))


def get_transcript_meta(user_id: int):
    with _conn() as c:
        return c.execute("SELECT * FROM transcripts WHERE user_id=%s",
                         (user_id,)).fetchone()