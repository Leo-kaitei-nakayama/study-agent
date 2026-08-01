#!/usr/bin/env python3
"""Study Agent — Webアプリ (Flask)。

サインアップ→メール確認(モック)→ログイン→プラン購入(モック決済)→
本人のステータス画面(ダッシュボード)、という流れ。

運営者(あなた)のマスターAPIキーは環境変数から読む:
  ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
学生は課金するだけで、鍵を自分で入力する必要はない。
"""
import os
import re
import sys
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, flash, g, redirect, render_template, request,
                   send_file, session, url_for)

sys.path.insert(0, str(Path(__file__).parent.parent))  # study_agent パッケージを見える化

import coursemap
import crypto
import db
import i18n
import mailer
import payments
import preview as preview_render
import school as school_info
import transcript as transcript_parser
import weeks
from i18n import t
from plans import DEFAULT_PLAN, PLANS
from study_agent import llm as study_llm

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

UPLOAD_DIR = Path(__file__).parent / "instance" / "uploads"
OUTPUT_DIR = Path(__file__).parent / "instance" / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MASTER_KEYS = {
    "claude": os.getenv("ANTHROPIC_API_KEY", ""),
    "openai": os.getenv("OPENAI_API_KEY", ""),
    "deepseek": os.getenv("DEEPSEEK_API_KEY", ""),
}
DEFAULT_ROUTING = {"math_cs": "claude", "multiple_choice": "openai",
                  "general": "openai"}  # DeepSeekは今は未使用

db.init_db()  # gunicorn は __main__ を実行しないので、ここで初期化しておく


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


# ------------------------------------------------------------------ 多言語
@app.before_request
def _resolve_language():
    """このリクエストで使う言語を決めて g.lang に入れる。

    優先順位: ?lang= → セッション → プロフィール → ブラウザ設定 → 英語。
    i18n.t() はここで決まった g.lang を見る。
    """
    lang = i18n.normalize(request.args.get("lang"))
    if not lang:
        lang = i18n.normalize(session.get("lang"))
    if not lang and session.get("user_id"):
        lang = i18n.normalize(db.get_lang(session["user_id"]))
    if not lang:
        lang = i18n.from_accept_language(request.headers.get("Accept-Language"))
    g.lang = lang or i18n.DEFAULT_LANG


@app.route("/lang/<code>")
def set_language(code):
    """言語切り替えボタンの飛び先。切り替えたら元のページへ戻す。

    ログイン済みならプロフィールにも保存するので、別の端末でも同じ言語になる。
    """
    lang = i18n.normalize(code)
    if lang:
        session["lang"] = lang
        if session.get("user_id"):
            db.set_lang(session["user_id"], lang)
    # next は同一サイト内のパスだけ許可する(外部サイトへの誘導を防ぐ)
    nxt = request.args.get("next", "")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("index")
    return redirect(nxt)


@app.context_processor
def _inject_i18n():
    """全テンプレートで t() と言語一覧を使えるようにする。"""
    return {"t": t, "LANGUAGES": i18n.LANGUAGES, "current_lang": i18n.get_locale()}


# --------------------------------------------------------------- auth flow
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("signup"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        username = request.form["username"].strip()
        if not email or not username:
            flash(t("flash.need_email_username"))
            return render_template("signup.html")
        existing = db.get_user_by_email(email)
        user_id = existing["id"] if existing else db.create_user(email, username)
        code = db.issue_code(user_id, "signup")
        mailer.send_verification_code(email, code, "signup")
        session["pending_user_id"] = user_id
        dev_code = code if mailer.DEV_MODE else None
        return redirect(url_for("verify", dev_code=dev_code))
    return render_template("signup.html")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    user_id = session.get("pending_user_id")
    if not user_id:
        return redirect(url_for("signup"))
    if request.method == "POST":
        code = request.form["code"].strip()
        if db.check_code(user_id, "signup", code):
            db.mark_verified(user_id)
            session.pop("pending_user_id", None)
            session["user_id"] = user_id
            if not db.get_profile(user_id):
                return redirect(url_for("onboarding"))
            if not db.get_subscription(user_id):
                return redirect(url_for("plans"))
            return redirect(url_for("dashboard"))
        flash(t("flash.bad_code"))
    dev_code = request.args.get("dev_code")
    return render_template("verify.html", dev_code=dev_code)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = db.get_user_by_email(email)
        if not user:
            flash(t("flash.no_account"))
            return render_template("login.html")
        code = db.issue_code(user["id"], "login")
        mailer.send_verification_code(email, code, "login")
        session["pending_login_user_id"] = user["id"]
        dev_code = code if mailer.DEV_MODE else None
        return redirect(url_for("login_verify", dev_code=dev_code))
    return render_template("login.html")


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    user_id = session.get("pending_login_user_id")
    if not user_id:
        return redirect(url_for("login"))
    if request.method == "POST":
        code = request.form["code"].strip()
        if db.check_code(user_id, "login", code):
            session.pop("pending_login_user_id", None)
            session["user_id"] = user_id
            return redirect(url_for("dashboard"))
        flash(t("flash.bad_code"))
    dev_code = request.args.get("dev_code")
    return render_template("verify.html", dev_code=dev_code, login=True)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------------------------------------------------- onboarding
@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    user_id = session["user_id"]
    if request.method == "POST":
        preferred_name = request.form["preferred_name"].strip()
        major = request.form.get("major", "").strip()
        school = request.form.get("school", "").strip()
        classes_raw = request.form.get("classes", "")
        if not preferred_name:
            flash(t("flash.need_name"))
            return render_template("onboarding.html")

        db.set_profile(user_id, preferred_name, major, school)
        # ここまで選んでいた言語をプロフィールにも残す(端末を変えても保つため)
        db.set_lang(user_id, i18n.get_locale())
        for line in classes_raw.splitlines():
            name = line.strip()
            if name:
                db.add_course(user_id, name)

        if not db.get_subscription(user_id):
            return redirect(url_for("plans"))
        return redirect(url_for("dashboard"))
    return render_template("onboarding.html")


def _term_tabs(user_id: int) -> tuple[list[str], str]:
    """ノート一覧に出すクォーターのタブと、いま選ばれているもの。

    ノートが存在するクォーター + 今のクォーターを、新しい順に並べる。
    選択は ?term= で指定。指定が無ければ今のクォーター(無ければ最新)。
    """
    known = set(db.list_note_terms(user_id))
    current = weeks.term_of()
    known.add(current)
    tabs = sorted(known, key=transcript_parser.term_sort_key, reverse=True)

    selected = request.args.get("term", "").strip()
    if selected not in tabs:
        selected = current if current in tabs else (tabs[0] if tabs else current)
    return tabs, selected


@app.route("/notes")
@login_required
def notes_library():
    """科目ごとのノート一覧。クォーターのタブで絞り込む。

    課題の一覧は /assignments に分けた(この画面はノートだけにするため)。
    """
    user_id = session["user_id"]
    tabs, selected = _term_tabs(user_id)
    counts = db.notes_count_by_course(user_id, term=selected)
    # ?edit=1 で iPhone のホーム画面のような編集モードになり、
    # 各タイルの角に ✕ が出て、その科目ごと消せる。
    return render_template(
        "notes_library.html",
        courses=db.list_user_courses(user_id),
        counts=counts,
        uncategorized_count=counts.get(None, 0),
        terms=tabs, selected_term=selected,
        edit_mode=request.args.get("edit") == "1")


@app.route("/notes/course/<int:course_id>/delete", methods=["GET", "POST"])
@login_required
def notes_course_delete(course_id):
    """科目のタイルごと消す(ノート・課題・リンクも一緒に)。

    GET で「何が消えるか」を見せ、POST で実際に消す。
    ログイン情報だけは消さず、科目との紐付けを外すだけにする
    (サイトのパスワードは科目を消した後も使うことがあるため)。
    """
    user_id = session["user_id"]
    course = db.get_course(user_id, course_id)
    if not course:
        flash(t("flash.course_missing"))
        return redirect(url_for("notes_library"))

    if request.method == "POST":
        removed = db.delete_course(user_id, course_id)
        _remove_output_files(removed)
        flash(t("flash.course_deleted", name=course["name"], count=len(removed)))
        return redirect(url_for("notes_library"))

    return render_template("notes_course_delete.html", course=course,
                          summary=db.course_delete_summary(user_id, course_id))


@app.route("/assignments")
@login_required
def assignments_page():
    """課題の一覧。今週ぶんを先頭に出し、その下に他の週を並べる。

    まとめて下書きさせるボタンもここに置く(ノート一覧から移した)。
    """
    user_id = session["user_id"]
    tabs, selected = _term_tabs(user_id)
    this_week = weeks.week_of()

    all_items = db.list_assignments(user_id, term=selected)
    current = [a for a in all_items if a["week_no"] == this_week]

    # 今週以外は週ごとにまとめる(新しい週が上)
    others: dict[int, list] = {}
    for a in all_items:
        if a["week_no"] != this_week:
            others.setdefault(a["week_no"] or 0, []).append(a)
    other_weeks = [{"week": w, "items": items}
                   for w, items in sorted(others.items(), reverse=True)]

    # 下書きは各行のボタンから 1 件ずつ。まとめて走らせる入口は置いていない
    # (オリエンテーション科目などで課題が 100 件を超えるため)。
    return render_template(
        "assignments.html",
        terms=tabs, selected_term=selected,
        this_week=this_week, week_assignments=current,
        other_weeks=other_weeks, total=len(all_items))


def _is_draftable(a) -> bool:
    """エージェントに下書きさせられる課題か。

    エッセイは「本人が計画する」と指定されているので対象外。
    すでに下書き済みのものも、押し間違いで作り直さないよう外す。
    """
    return a["kind"] != "essay" and a["status"] == "todo"


@app.route("/notes/course/<int:course_id>")
@login_required
def notes_course(course_id):
    user_id = session["user_id"]
    course = db.get_course(user_id, course_id)
    if not course:
        flash(t("flash.course_missing"))
        return redirect(url_for("notes_library"))
    tabs, selected = _term_tabs(user_id)
    # 科目の文脈カード: シラバスから読み取った道具と、その学期の課題を
    # 種類ごとに数えたもの(どの部品が担当するか = coursemap.allocate)。
    tools = coursemap.tools_from_json(course.get("tools"))
    duty: dict[str, int] = {}
    for a in db.list_assignments(user_id, term=selected, course_id=course_id):
        duty[a["kind"]] = duty.get(a["kind"], 0) + 1
    allocation = [{"kind": k, "count": n, "agent": coursemap.allocate(k)}
                  for k, n in sorted(duty.items(), key=lambda kv: -kv[1])]
    return render_template("notes_course.html", course=course,
                          notes=db.list_notes_for_course(user_id, course_id, selected),
                          terms=tabs, selected_term=selected,
                          tools=tools, allocation=allocation)


@app.route("/notes/uncategorized")
@login_required
def notes_uncategorized():
    user_id = session["user_id"]
    tabs, selected = _term_tabs(user_id)
    # 「未分類」は科目ではないので、文脈カード(道具・担当)は出さない
    return render_template("notes_course.html", course=None,
                          notes=db.list_notes_for_course(user_id, None, selected),
                          terms=tabs, selected_term=selected,
                          tools=[], allocation=[])


@app.route("/notes/view/<int:note_id>")
@login_required
def notes_view(note_id):
    """ダウンロードせずに中身を読む画面。

    .docx はテキストを取り出し、.md は軽く整形して HTML にする(preview.py)。
    """
    user_id = session["user_id"]
    row = db.get_note(user_id, note_id)
    if not row:
        flash(t("flash.note_missing"))
        return redirect(url_for("notes_library"))

    course = db.get_course(user_id, row["course_id"]) if row["course_id"] else None
    return render_template("note_view.html", note=row, course=course,
                          rendered=preview_render.render(OUTPUT_DIR / row["filename"]))


@app.route("/notes/download/<int:note_id>")
@login_required
def notes_download(note_id):
    user_id = session["user_id"]
    row = db.get_note(user_id, note_id)
    if not row:
        flash(t("flash.note_missing"))
        return redirect(url_for("notes_library"))
    # ダウンロード名は `Week 3: HW2.docx` のように、画面上の表示名に合わせる
    stem = row["title"] or row["source_name"]
    suffix = Path(row["filename"]).suffix
    safe = re.sub(r'[\\/:*?"<>|]+', "-", stem).strip() or "note"
    return send_file(OUTPUT_DIR / row["filename"], as_attachment=True,
                     download_name=f"{safe}{suffix}")


# ------------------------------------------------------------ ノートの削除
# 誤って全部消さないよう、3 段階に分ける:
#   1. /notes/delete        … 何を消すか選ぶ(個別 or その学期ぜんぶ)
#   2. /notes/delete/confirm … 消す一覧を見せて最終確認
#   3. /notes/delete/apply   … ここで初めて実際に消す
# ブラウザの confirm() だけに頼らないので、JS が効かない環境でも確認が挟まる。
@app.route("/notes/delete")
@login_required
def notes_delete():
    """削除するノートを選ぶ画面。"""
    user_id = session["user_id"]
    tabs, selected = _term_tabs(user_id)
    return render_template(
        "notes_delete.html",
        notes=db.list_notes_for_term(user_id, selected),
        terms=tabs, selected_term=selected)


@app.route("/notes/delete/confirm", methods=["POST"])
@login_required
def notes_delete_confirm():
    """本当に消すか確認する画面。ここではまだ何も消さない。"""
    user_id = session["user_id"]
    term = request.form.get("term", "").strip() or None
    scope = request.form.get("scope", "selected")

    if scope == "all":
        targets = db.list_notes_for_term(user_id, term)
    else:
        ids = [int(v) for v in request.form.getlist("note_id") if v.isdigit()]
        targets = db.get_notes_by_ids(user_id, ids)

    if not targets:
        flash(t("flash.nothing_selected"))
        return redirect(url_for("notes_delete", term=term))

    return render_template("notes_delete_confirm.html",
                          notes=targets, term=term, scope=scope)


@app.route("/notes/delete/apply", methods=["POST"])
@login_required
def notes_delete_apply():
    """確認画面から来たときだけ、実際に削除する。"""
    user_id = session["user_id"]
    term = request.form.get("term", "").strip() or None

    if request.form.get("scope") == "all":
        removed = db.delete_all_notes(user_id, term=term)
    else:
        ids = [int(v) for v in request.form.getlist("note_id") if v.isdigit()]
        removed = db.delete_notes(user_id, ids)

    _remove_output_files(removed)
    flash(t("flash.notes_deleted", count=len(removed)) if removed
          else t("flash.nothing_selected"))
    return redirect(url_for("notes_library", term=term))


#: 1 回のボタン操作で下書きする上限。押しっぱなしで際限なく課金されないための歯止め。
MAX_DRAFTS_PER_RUN = 5


@app.route("/notes/run-week", methods=["POST"])
@login_required
def notes_run_week():
    """選ばれた課題の下書きを作る。

    課題ページの各行にある小さなボタンから、その 1 件の id が送られてくる。
    id が 1 つも来なかった場合は何もしない(「全部やる」を暗黙に走らせない
    — 意図しない課金を避けるため)。複数来ても受け付けるが、1 回で作るのは
    MAX_DRAFTS_PER_RUN 件まで。

    PDF の指定どおり:
      - quiz / short は、その週のノートを根拠に下書きする
      - **essay は扱わない**(本人が計画すると指定されているため)
      - **提出は一切しない。** できるのは下書きまでで、出すかどうかは本人が決める
    """
    user_id = session["user_id"]
    if not db.has_credit(user_id):
        flash(t("flash.no_credit"))
        return redirect(url_for("plans"))

    term = request.form.get("term", "").strip() or weeks.term_of()
    ids = [int(v) for v in request.form.getlist("assignment_id") if v.isdigit()]
    pending = [a for a in db.get_assignments_by_ids(user_id, ids) if _is_draftable(a)]
    if not pending:
        flash(t("flash.week_nothing"))
        return redirect(url_for("assignments_page", term=term))

    routing = _user_routing(user_id)
    done, failed = 0, 0
    # 週ごとのノートは使い回す(同じ週の課題を続けて処理することが多いため)
    contexts: dict[tuple, str] = {}

    for item in pending[:MAX_DRAFTS_PER_RUN]:
        item_term = item["term"] or term
        item_week = item["week_no"] or weeks.week_of()
        key = (item_term, item_week)
        if key not in contexts:
            contexts[key] = _week_notes_context(user_id, item_term, item_week)
        try:
            _draft_one_assignment(user_id, item, contexts[key], routing,
                                  item_term, item_week)
            done += 1
        except Exception as e:  # noqa: BLE001 — 1件失敗しても残りは続ける
            app.logger.warning("下書き失敗 %s: %s", item["name"], e)
            failed += 1
        if not db.has_credit(user_id):
            break   # 途中で残高が尽きたらそこで止める

    flash(t("flash.week_drafted", count=done) if done else t("flash.week_failed"))
    if failed:
        flash(t("flash.week_partial", count=failed))
    return redirect(url_for("assignments_page", term=term))


def _week_notes_context(user_id: int, term: str, week_no: int,
                        limit_chars: int = 12000) -> str:
    """その週のノートを 1 つの文字列にまとめる(下書きの根拠にする)。

    クイズはその日のノートを根拠に答える、という設計なので、ここで集めた
    ものだけを「ノート由来」の材料として渡す。
    """
    chunks: list[str] = []
    for note in db.list_notes_for_course(user_id, None, term):
        chunks.append(note)
    # 科目つきのノートも含める
    for course in db.list_user_courses(user_id):
        for note in db.list_notes_for_course(user_id, course["id"], term):
            chunks.append(note)

    texts: list[str] = []
    total = 0
    for note in chunks:
        if note["week_no"] not in (None, week_no):
            continue
        if note["kind"] not in ("notes", "resource", "syllabus"):
            continue
        try:
            body, _ = preview_render.load_text(OUTPUT_DIR / note["filename"])
        except Exception:  # noqa: BLE001 — 読めないノートは黙って飛ばす
            continue
        piece = f"--- {note['title'] or note['source_name']} ---\n{body}"
        texts.append(piece)
        total += len(piece)
        if total >= limit_chars:
            break
    return "\n\n".join(texts)[:limit_chars]


def _draft_one_assignment(user_id: int, item, context: str, routing: dict,
                          term: str, week_no: int):
    """課題 1 件の下書きを作って保存し、状態を drafted にする。"""
    system = (
        "You are a study tutor helping a university student prepare a DRAFT "
        "answer for a practice assignment. Ground your answer in the student's "
        "own notes when they cover the question, and mark anything that goes "
        "beyond the notes so the student can verify it. Never claim the work is "
        "finished or submitted — this is a draft the student will review, edit "
        "and submit themselves. "
        f"Write the whole answer in {i18n.ai_output_lang()}.")

    parts = [f"# Assignment: {item['name']}"]
    if item.get("course_name"):
        parts.append(f"Course: {item['course_name']}")
    if item.get("due_at"):
        parts.append(f"Due: {item['due_at']}")
    if item.get("description"):
        parts.append(f"\nInstructions:\n{item['description']}")
    parts.append(f"\nThis week's notes:\n{context or '(no notes for this week yet)'}")

    answer = study_llm.complete(
        system, "\n".join(parts), max_tokens=4000, api_keys=MASTER_KEYS,
        usage_callback=_make_usage_callback(user_id, "week_draft"),
        routing=routing)

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", item["name"])[:40] or "assignment"
    filename = f"{user_id}_{stamp}_draft_{safe}.md"
    banner = ("> **DRAFT — not submitted.** Review and edit before you hand this in.\n\n")
    (OUTPUT_DIR / filename).write_text(
        f"# {item['name']}\n\n{banner}{answer}\n", encoding="utf-8")

    note_id = db.add_note(user_id, item["course_id"], "draft", filename,
                          item["name"],
                          title=weeks.note_title(item["name"], week=week_no),
                          week_no=week_no, term=term)
    db.set_assignment_status(user_id, item["id"], "drafted", note_id)


def _remove_output_files(filenames: list[str]):
    """DB から消したノートの実ファイルも片付ける。

    消えていても構わないので、失敗しても例外にしない
    (DB 上は既に消えているため、画面の整合性は保たれている)。
    """
    for name in filenames:
        try:
            (OUTPUT_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


# ------------------------------------------------------------ extension
@app.route("/extension/token", methods=["POST"])
@login_required
def generate_extension_token():
    """新しい拡張機能連携トークンを発行(既存は失効)。生の値はこの応答でしか見えない。"""
    import hashlib
    import secrets

    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    db.create_extension_token(session["user_id"], token_hash)
    return _render_dashboard(new_extension_token=raw)


def _extension_user_id():
    """Authorization: Bearer <token> からuser_idを引く。無効ならNone。"""
    import hashlib

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return db.get_user_by_extension_token_hash(token_hash)


def _extension_course_id(user_id: int, course_name: str | None):
    """拡張機能が言ってきた科目名から course_id を引く。

    Canvas の画面名(Dashboard / Inbox など)は授業ではないので取り込まず、
    None を返す(= その分は「未分類」に入る)。content.js はページのタイトル
    から科目名を推測するので、ダッシュボードを開いているとこれらが紛れ込む。
    """
    name = (course_name or "").strip()
    if not coursemap.is_real_course(name):
        return None
    return db.add_course(user_id, name)


@app.route("/api/extension/syllabus", methods=["POST"])
def api_extension_syllabus():
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    payload = request.get_json(silent=True) or {}
    course_name = (payload.get("course_name") or "").strip()
    text = (payload.get("text") or "").strip()
    if not course_name or not text:
        return {"error": "course_name and text are required"}, 400

    course_id = _extension_course_id(user_id, course_name)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    filename = f"{user_id}_{stamp}_syllabus_{course_name}.md".replace(" ", "_")
    (OUTPUT_DIR / filename).write_text(text, encoding="utf-8")
    db.add_note(user_id, course_id, "syllabus", filename, f"{course_name} syllabus",
                title=f"{course_name} syllabus", term=weeks.term_of())
    return {"status": "ok"}


@app.route("/api/extension/state", methods=["GET"])
def api_extension_state():
    """拡張機能が「すでにサーバーが持っている情報」を確認するための入口。
    これを見て、取得済みのシラバスは再取得しない。"""
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    courses = db.list_user_courses(user_id)
    synced = []
    for c in courses:
        notes = db.list_notes_for_course(user_id, c["id"])
        if any(n["kind"] == "syllabus" for n in notes):
            synced.append(c["name"])
    pending = db.list_pending_links(user_id)
    return {"courses": [c["name"] for c in courses], "synced_syllabi": synced,
            # 「どの課題を、Canvas のいつ時点で取り込んだか」。
            # 拡張機能はこれと Canvas の updated_at を比べ、変わっていない
            # 課題を送らずに済ませる(差分同期の判断材料)。
            "assignment_state": db.assignment_sync_state(user_id),
            "pending_links": [
                {"url": p["url"], "label": p["label"],
                 "course": p["course_name"],
                 "is_thin_syllabus": bool(p["is_thin_syllabus"])}
                for p in pending]}


@app.route("/api/extension/sync", methods=["POST"])
def api_extension_sync():
    """科目・シラバス・課題をまとめて受け取る(自動モードの受け口)。"""
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    payload = request.get_json(silent=True) or {}
    courses = payload.get("courses") or []
    if not isinstance(courses, list):
        return {"error": "courses must be a list"}, 400

    saved_syllabi = 0
    saved_assignments = 0
    saved_assignment_rows = 0
    saved_modules = 0
    # 拡張機能からの呼び出しにはセッションが無いので、作るノートの言語は
    # プロフィールに保存されたものを使う(既定は英語)。
    user_lang = db.get_lang(user_id)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    for entry in courses:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        # Canvas の画面名(Dashboard など)は授業ではないので取り込まない
        if not coursemap.is_real_course(name):
            continue
        course_id = db.add_course(user_id, name)

        # シラバス内の外部リンクを記録(本体が外部サイトにある科目への対応)
        for link in (entry.get("syllabus_links") or []):
            if not isinstance(link, dict):
                continue
            url = (link.get("url") or "").strip()
            if not url:
                continue
            db.add_external_link(user_id, course_id, url,
                                (link.get("label") or "")[:200],
                                bool(entry.get("syllabus_is_thin")))

        syllabus = (entry.get("syllabus") or "").strip()
        if syllabus:
            fn = f"{user_id}_{stamp}_syllabus_{course_id}.md"
            (OUTPUT_DIR / fn).write_text(syllabus, encoding="utf-8")
            syl_title = f"{name} — {i18n.t_in(user_lang, 'notes.kind_syllabus')}"
            db.add_note(user_id, course_id, "syllabus", fn, syl_title,
                        title=syl_title, term=weeks.term_of())
            saved_syllabi += 1

        # 週ごとのモジュール構成。ノート(kind='modules')として残す。
        modules = entry.get("modules") or []
        if modules:
            mod_title = f"{name} — {i18n.t_in(user_lang, 'notes.kind_modules')}"
            mod_lines = [f"# {mod_title}", ""]
            for m in modules:
                if not isinstance(m, dict):
                    continue
                mod_lines.append(f"## {m.get('name') or '(no name)'}")
                for it in (m.get("items") or []):
                    if isinstance(it, dict) and it.get("title"):
                        mod_lines.append(f"- {it['title']}")
                mod_lines.append("")
            fn = f"{user_id}_{stamp}_modules_{course_id}.md"
            (OUTPUT_DIR / fn).write_text("\n".join(mod_lines), encoding="utf-8")
            db.add_note(user_id, course_id, "modules", fn,
                        mod_title, title=mod_title,
                        term=weeks.term_of())
            saved_modules += 1

        # 科目の文脈: Canvas の科目 ID と、シラバスから読み取った道具。
        # 道具の抽出は語彙表との照合だけで、LLM は呼ばない(coursemap.py)。
        canvas_id = entry.get("canvas_id")
        tools_json = None
        if syllabus:
            tools = coursemap.extract_tools(syllabus)
            if tools:
                tools_json = coursemap.tools_to_json(tools)
        if canvas_id or tools_json:
            try:
                cid = int(canvas_id) if canvas_id is not None else None
            except (TypeError, ValueError):
                cid = None
            db.set_course_context(user_id, course_id, canvas_id=cid,
                                  tools=tools_json)

        # partial = 変更のあった課題だけが送られてきた差分同期。
        # このとき一覧ノートを作り直すと、変わっていない課題が消えてしまうので
        # 表への取り込みだけ行い、まとめノートは触らない。
        is_partial = bool(entry.get("partial"))
        assignments = entry.get("assignments") or []
        if assignments:
            # 一覧ノートは「ぱっと見て分かる表」にする。説明文はここには
            # 入れない — 1 件が数百字になることがあり、100 件並ぶと
            # 一覧として読めなくなるため。説明文は assignments.description
            # に入っていて、下書きを作るときにそこから読む。
            rows: list[tuple] = []
            for a in assignments:
                if not isinstance(a, dict):
                    continue
                a_name = a.get("name") or "(名称なし)"
                desc = (a.get("description") or "").strip()

                # 課題そのものも 1 件ずつ表に入れる(「今週の課題」の材料)。
                # 週番号は Canvas の書き方(名前の "Week 3")を最優先し、
                # 無ければ締切日から求める(weeks.py)。
                due_at = a.get("due_at") or None
                a_canvas_id = a.get("id")
                try:
                    a_canvas_id = int(a_canvas_id) if a_canvas_id is not None else None
                except (TypeError, ValueError):
                    a_canvas_id = None
                db.upsert_assignment(
                    user_id, course_id, a_name, due_at=due_at,
                    points=a.get("points"), url=a.get("url"),
                    description=desc[:4000] or None,
                    week_no=weeks.week_of(a_name, due_at),
                    term=weeks.term_of(due_at),
                    kind=_assignment_kind(a_name, desc),
                    canvas_id=a_canvas_id,
                    canvas_updated_at=a.get("updated_at") or None,
                    rubric=(a.get("rubric") or "").strip()[:4000] or None)
                saved_assignment_rows += 1
                rows.append((weeks.week_of(a_name, due_at), a_name, due_at,
                             a.get("points"), _assignment_kind(a_name, desc),
                             bool((a.get("rubric") or "").strip())))

            if not is_partial:
                # ノートの名前も利用者の言語で。直書きすると英語の画面に
                # 「課題一覧」と出る(以前 school.py で起きた不具合と同じ)。
                list_title = f"{name} — {i18n.t_in(user_lang, 'notes.kind_assignments')}"
                fn = f"{user_id}_{stamp}_assignments_{course_id}.md"
                (OUTPUT_DIR / fn).write_text(
                    _assignment_list_markdown(name, rows, user_lang),
                    encoding="utf-8")
                db.add_note(user_id, course_id, "assignments", fn,
                           list_title, title=list_title,
                           term=weeks.term_of())
                saved_assignments += 1

    # フルクロールは「選んだ学期の科目はこれで全部」という意味なので、
    # そこに出てこなかった科目は前の学期のもの。学期を絞る前に取り込んで
    # しまったぶんをここで片付ける(中身のあるものは残る)。
    pruned: list[dict] = []
    if payload.get("full") and courses:
        keep = [(e.get("name") or "").strip()
                for e in courses if isinstance(e, dict)]
        pruned = db.prune_synced_courses(user_id, keep)
        for p in pruned:
            _remove_output_files(p["files"])

    pending = db.list_pending_links(user_id, thin_only=True)
    return {"status": "ok", "courses": len(courses),
            "syllabi": saved_syllabi, "assignments": saved_assignments,
            "modules": saved_modules,
            "assignment_rows": saved_assignment_rows,
            "pruned": [p["name"] for p in pruned],
            "pending_external_syllabi": [
                {"url": p["url"], "label": p["label"],
                 "course": p["course_name"]} for p in pending]}


def _assignment_list_markdown(course_name: str, rows: list[tuple],
                              lang: str | None = None) -> str:
    """課題一覧ノートの中身を作る。**一覧であって、課題の中身ではない。**

    1 行 1 件の表にして、週ごとにまとめる。説明文は入れない — Canvas の
    説明は 1 件で数百字あることが珍しくなく、100 件並ぶと「一覧」として
    読めなくなるため。中身が要るときは Canvas の元ページか、下書きを
    作らせたときの生成物を見る。

    見出しの文字は i18n から取る。ここは拡張機能から呼ばれてセッションが
    無いので、`t()` ではなく `i18n.t_in(lang, ...)` を使う(lang は
    プロフィールに保存された言語)。直書きすると英語の画面に日本語の表が
    出てしまう。

    rows は (週, 名前, 締切, 配点, 種類, ルーブリック有無) の並び。
    """
    def tr(key, **kw):
        return i18n.t_in(lang, key, **kw)

    out = [f"# {course_name}", "",
           tr("notes.list_summary", count=len(rows)), ""]

    # 週ごと。週が付かなかったものは最後に「週なし」でまとめる。
    by_week: dict[int, list[tuple]] = {}
    for r in rows:
        by_week.setdefault(r[0] or 0, []).append(r)

    header = (f"| {tr('notes.col_assignment')} | {tr('notes.col_due')} "
              f"| {tr('notes.col_points')} | {tr('notes.col_kind')} |")
    for week in sorted(by_week):
        items = sorted(by_week[week], key=lambda r: (r[2] is None, r[2] or ""))
        out.append("## " + (tr("notes.week_label", n=week) if week
                            else tr("notes.no_due")))
        out.append("")
        out.append(header)
        out.append("|---|---|---|---|")
        for _, a_name, due, points, kind, has_rubric in items:
            label = tr(f"notes.akind_{kind}")
            if has_rubric:
                label += " ·📋"          # ルーブリックが付いている印
            out.append(
                f"| {_md_cell(a_name)} | {(due or '')[:10] or '—'} "
                f"| {'' if points is None else points} | {label} |")
        out.append("")

    return "\n".join(out)


def _md_cell(text: str) -> str:
    """表のセルに入れられる形にする(| と改行を潰す)。"""
    return " ".join(str(text).split()).replace("|", "/")


# 課題の種類を、名前と説明文から見分ける。PDF の指定どおり:
#   quiz  … 画像で送られてくる選択式 → ノートを根拠に答える
#   short … 短い記述 → その週のノートから下書きを作る
#   essay … 長文エッセイ → **今は扱わない**(本人が書く)
_QUIZ_WORDS = re.compile(
    r"\b(quiz|exam|midterm|final|test|multiple[\s-]?choice)\b", re.IGNORECASE)
_ESSAY_WORDS = re.compile(
    r"\b(essay|paper|report|thesis|\d{3,}\s*words?)\b", re.IGNORECASE)
# 説明文の中を見るとき用。"report" / "paper" は普通の文章にもよく出るので
# 外してある(名前に入っていれば _ESSAY_WORDS のほうで拾える)。
_ESSAY_WORDS_BODY = re.compile(
    r"\b(essay|thesis|\d{3,}\s*words?)\b", re.IGNORECASE)
_SHORT_WORDS = re.compile(
    r"\b(short\s+(answer|response)|discussion|reflection|reading\s+response|"
    r"annotation|comment)\b", re.IGNORECASE)


def _assignment_kind(name: str, description: str = "") -> str:
    """課題の種類を判定する。判断がつかないものは 'other'。

    essay は「あとで自分で計画する」と指定されているので、
    エージェントが自動で書き始めないよう、ここで明示的に分けておく。
    """
    # **課題名を先に、単独で見る。** 名前と説明文をつないで一度に調べると、
    # Canvas の長い説明にたまたま出てくる "report" や "paper" のせいで、
    # 「Week 2: Inverted index quiz」までエッセイ扱いになってしまう。
    # そうなると下書きの対象から外れて、ボタンが出なくなる。
    if _ESSAY_WORDS.search(name or ""):
        return "essay"
    if _QUIZ_WORDS.search(name or ""):
        return "quiz"
    if _SHORT_WORDS.search(name or ""):
        return "short"

    # 名前で決まらなかったときだけ説明文を見る。ただしエッセイ判定は
    # 強い言い方(essay / thesis / 「500 words」)に限る。"report" や
    # "paper" は普通の文章にもよく出るので、ここでは根拠にしない。
    body = description or ""
    if _ESSAY_WORDS_BODY.search(body):
        return "essay"
    if _QUIZ_WORDS.search(body):
        return "quiz"
    if _SHORT_WORDS.search(body):
        return "short"
    return "other"


@app.route("/api/extension/links", methods=["POST"])
def api_extension_links():
    """content.js がページ上で常時検知したリンクをまとめて報告する。
    ここから他のページに追加でアクセスすることはない(受け取って記録するだけ)。"""
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    payload = request.get_json(silent=True) or {}
    links = payload.get("links") or []
    if not isinstance(links, list):
        return {"error": "links must be a list"}, 400

    course_name = (payload.get("course_name") or "").strip()
    course_id = _extension_course_id(user_id, course_name)

    saved = 0
    for link in links:
        if not isinstance(link, dict):
            continue
        url = (link.get("url") or "").strip()
        if not url:
            continue
        db.add_external_link(user_id, course_id, url,
                             (link.get("label") or "")[:200], False)
        saved += 1

    return {"status": "ok", "saved": saved}


@app.route("/api/extension/capture", methods=["POST"])
def api_extension_capture():
    """任意のサイトのページを取り込む(手動モード)。
    ユーザーがボタンを押した時だけ拡張機能から送られてくる。"""
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return {"error": "text is required"}, 400

    url = (payload.get("url") or "").strip()
    title = (payload.get("title") or "").strip() or url or "取り込んだページ"
    course_name = (payload.get("course_name") or "").strip()
    kind = payload.get("kind") or "resource"
    if kind not in ("resource", "requirements"):
        kind = "resource"

    course_id = _extension_course_id(user_id, course_name)

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", title)[:40] or "page"
    fn = f"{user_id}_{stamp}_{kind}_{safe}.md"
    body = f"# {title}\n\n出典: {url}\n\n---\n\n{text}"
    (OUTPUT_DIR / fn).write_text(body, encoding="utf-8")
    db.add_note(user_id, course_id, kind, fn, title,
                title=title, term=weeks.term_of())
    resolved = db.mark_link_captured(user_id, url) if url else False

    return {"status": "ok", "chars": len(text), "title": title,
            "resolved_pending_link": resolved}


@app.route("/api/extension/answer", methods=["POST"])
def api_extension_answer():
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    payload = request.get_json(silent=True) or {}
    course_name = (payload.get("course_name") or "").strip()
    question_text = (payload.get("question_text") or "").strip()
    if not question_text:
        return {"error": "question_text is required"}, 400

    sub = db.get_subscription(user_id)
    if not sub:
        return {"error": "no active plan"}, 402

    cb = _make_usage_callback(user_id, "extension_answer")
    routing = _user_routing(user_id)
    system = (
        "あなたは大学の練習問題を手伝うチューターです。以下の設問に対する解答の"
        "下書きを作成してください。解答のみを簡潔に出力し、前置きは不要です。"
        "出力言語は設問と同じ言語にしてください。")
    try:
        answer = study_llm.complete(system, question_text, max_tokens=1000,
                                    api_keys=MASTER_KEYS, usage_callback=cb,
                                    routing=routing)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, 500

    course_id = _extension_course_id(user_id, course_name)
    if course_id:
        db.add_note(user_id, course_id, "practice_answer",
                   f"_inline_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.md",
                   question_text[:60],
                   title=weeks.note_title(question_text[:60]),
                   week_no=weeks.week_of(), term=weeks.term_of())
    return {"answer": answer}


# ------------------------------------------- 拡張機能: スクリーンショット
# PDF の指定:
#   - アイコンを押すだけで画面を撮って、何をしてほしいかを選ぶ
#     (explanation / answer / other = 自由入力)
#   - **Canvas では撮らない。** Canvas は学生自身が操作すると指定されているため、
#     エージェント側からは触らない。Perusall のように本文を取れないサイトで、
#     読み物を要約したりコメントを書いたりするために使う。
SCREENSHOT_MODES = ("explanation", "answer", "other")

#: スクリーンショットを撮らせないホスト(部分一致)。
BLOCKED_SCREENSHOT_HOSTS = ("canvas.eee.uci.edu", "instructure.com")

#: 受け取る画像の上限(だいたい 8MB のデータURL)。
MAX_SCREENSHOT_CHARS = 8_000_000


def _screenshot_blocked(url: str) -> bool:
    """そのページで撮ってよいか。Canvas 系は不可。"""
    host = urlparse(url or "").hostname or ""
    return any(blocked in host for blocked in BLOCKED_SCREENSHOT_HOSTS)


@app.route("/api/extension/screenshot", methods=["POST"])
def api_extension_screenshot():
    """スクリーンショット + 「何をしてほしいか」を受け取って答える。

    mode:
      explanation … 何が書いてあるか / どう考えるかを説明する
      answer      … 解答の下書きを作る(提出はしない)
      other       … prompt に書かれた指示に従う
    """
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401
    if not db.has_credit(user_id):
        return {"error": "no credit left"}, 402

    payload = request.get_json(silent=True) or {}
    page_url = (payload.get("url") or "").strip()
    if _screenshot_blocked(page_url):
        return {"error": "screenshots are disabled on Canvas",
                "reason": "canvas_blocked"}, 403

    image = (payload.get("image") or "").strip()
    if not image:
        return {"error": "image is required"}, 400
    if len(image) > MAX_SCREENSHOT_CHARS:
        return {"error": "image too large"}, 413

    # data:image/png;base64,xxxx → media_type と本体に分ける
    m = re.match(r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", image, re.S)
    if not m:
        return {"error": "image must be a base64 data URL"}, 400
    media_type, image_b64 = m.group(1), m.group(2)

    mode = payload.get("mode") if payload.get("mode") in SCREENSHOT_MODES else "explanation"
    custom = (payload.get("prompt") or "").strip()
    course_name = (payload.get("course_name") or "").strip()

    instruction = {
        "explanation": "Explain what this page is asking and how to think about it. "
                       "Do not just give a final answer — show the reasoning.",
        "answer": "Draft an answer to what is on this page. This is a DRAFT for the "
                  "student to review and edit; never present it as submitted work.",
        "other": custom or "Describe what is on this page.",
    }[mode]

    system = (
        "You are a study tutor looking at a screenshot of a student's coursework. "
        + instruction +
        " If the screenshot is unreadable or does not contain a question, say so "
        "plainly instead of guessing. "
        f"Write your reply in {i18n.ai_output_lang()}.")

    try:
        answer = study_llm.complete_vision(
            system, custom or instruction, image_b64, media_type,
            api_keys=MASTER_KEYS,
            usage_callback=_make_usage_callback(user_id, f"screenshot_{mode}"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, 500

    # あとで見返せるようにノートとしても残す
    course_id = _extension_course_id(user_id, course_name)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    filename = f"{user_id}_{stamp}_screenshot_{mode}.md"
    (OUTPUT_DIR / filename).write_text(
        f"# {t('ext.screenshot_note_title')} ({mode})\n\n"
        f"{page_url}\n\n---\n\n{answer}\n", encoding="utf-8")
    week_no = weeks.week_of()
    db.add_note(user_id, course_id, "screenshot", filename,
                f"screenshot ({mode})",
                title=weeks.note_title(f"screenshot ({mode})", week=week_no),
                week_no=week_no, term=weeks.term_of())

    return {"answer": answer, "mode": mode}


@app.route("/api/extension/notifications", methods=["GET"])
def api_extension_notifications():
    """下書きができたのにまだ知らせていない課題を返す(拡張機能が定期的に取りに来る)。"""
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401
    pending = db.list_pending_notifications(user_id)
    return {"pending": [
        {"id": p["id"], "name": p["name"], "course": p["course_name"],
         "url": url_for("notes_view", note_id=p["note_id"], _external=True)
                if p["note_id"] else None}
        for p in pending]}


@app.route("/api/extension/notifications/ack", methods=["POST"])
def api_extension_notifications_ack():
    """通知を出し終えたことを記録する(同じ通知を繰り返さないため)。"""
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401
    payload = request.get_json(silent=True) or {}
    ids = [int(v) for v in (payload.get("ids") or [])
           if str(v).isdigit()]
    return {"status": "ok", "marked": db.mark_notified(user_id, ids)}


@app.route("/api/extension/week", methods=["GET"])
def api_extension_week():
    """今週の課題をまとめて返す(拡張機能が月曜の朝に1件だけ通知するため)。

    返すのは一覧だけで、下書きは作らない。何を手伝わせるかは
    /assignments で本人が選ぶ、という流れを崩さないため。
    """
    user_id = _extension_user_id()
    if not user_id:
        return {"error": "invalid or missing token"}, 401

    term = weeks.term_of()
    week_no = weeks.week_of()
    items = []
    for a in db.list_assignments(user_id, term=term, week_no=week_no):
        # すでに下書き済みのものは「今週やること」から外す
        if a["status"] == "drafted":
            continue
        items.append({
            "id": a["id"],
            "name": a["name"],
            "course": a["course_name"] or "",
            "due": (a["due_at"] or "")[:10],
            "kind": a["kind"],
            # エージェントが担当できる種類か(エッセイは空になる)
            "agent": coursemap.allocate(a["kind"]),
        })
    return {"term": term, "week_no": week_no, "items": items,
            "url": url_for("assignments_page", _external=True)}


# ------------------------------------------------------------------ plans
@app.route("/plans", methods=["GET", "POST"])
@login_required
def plans():
    """プラン購入 = クレジットのチャージ。決済は payments.py のモック。"""
    user_id = session["user_id"]
    if request.method == "POST":
        plan = request.form["plan"]
        if plan not in PLANS:
            flash(t("flash.unknown_plan"))
            return redirect(url_for("plans"))
        ok = payments.charge(user_id, plan, PLANS[plan]["price_usd"])
        if ok:
            db.set_plan(user_id, plan)   # プラン名の記録(利用可否の判定には使わない)
            new_balance = db.add_credit(user_id, PLANS[plan]["credit_usd"],
                                        f"チャージ: {PLANS[plan]['label']}")
            flash(t("flash.charged",
                      amount=f"{PLANS[plan]['credit_usd']:.2f}",
                      balance=f"{new_balance:.2f}"))
            return redirect(url_for("dashboard"))
        flash(t("flash.payment_failed"))
    return render_template("plans.html", plans=PLANS, default=DEFAULT_PLAN,
                          balance=db.get_balance(user_id))


# ------------------------------------------------------- 連携サービス(ログイン情報)
@app.route("/services")
@login_required
def services():
    """授業で使うサイトのログイン情報を一覧・登録する画面。

    ここに登録された username/password を使って、エージェントが後から
    そのサイトに自分でログインして情報を取りに行く。パスワードは
    crypto.encrypt() を通してから DB に入れ、画面には二度と表示しない。
    """
    user_id = session["user_id"]
    return render_template(
        "services.html",
        credentials=db.list_site_credentials(user_id),
        courses=db.list_user_courses(user_id),
        crypto_ready=crypto.is_configured())


@app.route("/services/add", methods=["POST"])
@login_required
def services_add():
    user_id = session["user_id"]
    if not crypto.is_configured():
        flash(t("flash.no_crypto_key"))
        return redirect(url_for("services"))

    label = request.form.get("label", "").strip()
    site_url = request.form.get("site_url", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    course_choice = request.form.get("course", "").strip()

    if not (label and site_url and username and password):
        flash(t("flash.cred_fields_required"))
        return redirect(url_for("services"))
    if not site_url.startswith(("http://", "https://")):
        site_url = "https://" + site_url

    course_id = None
    if course_choice and course_choice != "__none__":
        course_id = db.add_course(user_id, course_choice)

    db.add_site_credential(user_id, label, site_url, username,
                           crypto.encrypt(password), course_id)
    flash(t("flash.cred_saved", label=label))
    return redirect(url_for("services"))


@app.route("/services/delete/<int:cred_id>", methods=["POST"])
@login_required
def services_delete(cred_id):
    db.delete_site_credential(session["user_id"], cred_id)
    flash(t("flash.cred_deleted"))
    return redirect(url_for("services"))


# ------------------------------------------------------------ 学業 / GPA
@app.route("/academic")
@login_required
def academic():
    """成績表アップロード → GPA と卒業単位までの進捗を表示する画面。

    履修データは transcript_courses テーブルに入っていて、GPA の計算そのものは
    transcript.py が行う(この関数は取り出して渡すだけ)。
    """
    user_id = session["user_id"]
    profile = db.get_profile(user_id)
    if not profile:
        return redirect(url_for("onboarding"))

    sch = school_info.get_school(profile["school"])
    rows = db.list_transcript_courses(user_id)
    # 公式集計(成績表末尾の UC GPA など)があればそちらを見出しに使う
    official = db.get_official_totals(user_id)
    terms = transcript_parser.group_by_term(rows, profile["school"])

    # 既定では最新の学期だけを開き、残りは「すべて表示」で開く。
    # 7 学期ぶん全部を常に出すと画面が長くなりすぎるため。
    show_all = request.args.get("all") == "1"
    return render_template(
        "academic.html",
        school=sch,
        meta=db.get_transcript_meta(user_id),
        summary=transcript_parser.compute_gpa(rows, profile["school"], official),
        terms=terms,
        visible_terms=terms if show_all else terms[:1],
        show_all=show_all,
        hidden_count=max(0, len(terms) - 1),
        in_progress_label=transcript_parser.IN_PROGRESS,
        has_data=bool(rows))


@app.route("/academic/upload", methods=["POST"])
@login_required
def academic_upload():
    """Student Access で保存した Unofficial Transcript (.html) を取り込む。"""
    user_id = session["user_id"]
    profile = db.get_profile(user_id)
    f = request.files.get("file")
    if not f or not f.filename:
        flash(t("flash.transcript_missing"))
        return redirect(url_for("academic"))

    raw = f.read()
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Student Access の保存 HTML は環境によって cp1252 などになることがある
        html = raw.decode("latin-1", errors="replace")

    result = transcript_parser.parse_transcript_html(
        html, profile["school"] if profile else None)
    if result["error"]:
        flash(result["error"])
        return redirect(url_for("academic"))

    db.replace_transcript(user_id, f.filename, result["courses"],
                          result.get("official"))
    flash(t("flash.transcript_imported", count=len(result["courses"]),
                terms=", ".join(result["terms"])))
    return redirect(url_for("academic"))


@app.route("/academic/grade/<int:row_id>", methods=["POST"])
@login_required
def academic_set_grade(row_id):
    """履修中の科目に、成績が出たあとで成績を入力する。"""
    user_id = session["user_id"]
    grade = request.form.get("grade", "").strip().upper()
    if not grade:
        flash(t("flash.manual_fields_required"))
    elif db.set_transcript_grade(user_id, row_id, grade):
        flash(t("flash.grade_saved", grade=grade))
    return redirect(url_for("academic", all=request.form.get("all") or None))


@app.route("/academic/manual", methods=["POST"])
@login_required
def academic_manual():
    """HTML の解析に失敗したとき用に、履修を 1 件ずつ手入力で足す。

    成績を空のまま出すと「履修中(N/A)」として登録され、あとから入力できる。
    """
    user_id = session["user_id"]
    term = request.form.get("term", "").strip()
    code = request.form.get("code", "").strip().upper()
    title = request.form.get("title", "").strip()
    grade = request.form.get("grade", "").strip().upper() or transcript_parser.IN_PROGRESS
    try:
        units = float(request.form.get("units", ""))
    except ValueError:
        flash(t("flash.units_numeric"))
        return redirect(url_for("academic"))

    if not (term and code) or units <= 0:
        flash(t("flash.manual_fields_required"))
        return redirect(url_for("academic"))

    db.add_transcript_course(user_id, term, code, title, units, grade)
    flash(t("flash.manual_added", code=code))
    return redirect(url_for("academic"))


@app.route("/academic/delete/<int:row_id>", methods=["POST"])
@login_required
def academic_delete(row_id):
    db.delete_transcript_course(session["user_id"], row_id)
    return redirect(url_for("academic"))


# -------------------------------------------------------------- dashboard
@app.route("/dashboard")
@login_required
def dashboard():
    return _render_dashboard()


def _render_dashboard(new_extension_token: str | None = None):
    user_id = session["user_id"]
    user = db.get_user(user_id)
    profile = db.get_profile(user_id)
    if not profile:
        return redirect(url_for("onboarding"))
    sub = db.get_subscription(user_id)
    if not sub:
        return redirect(url_for("plans"))
    usage = db.usage_this_cycle(user_id)
    courses = db.list_user_courses(user_id)
    counts = db.notes_count_by_course(user_id)

    return render_template(
        "dashboard.html", user=user, profile=profile, sub=sub,
        plan=PLANS[sub["plan"]],
        usage_count=len(usage),
        cost=_estimate_cost(usage),          # 今サイクルの利用額(参考表示)
        balance=db.get_balance(user_id),     # 画面に出すのはトークンではなく残高
        courses=courses, counts=counts,
        credentials=db.list_site_credentials(user_id),
        school=school_info.get_school(profile["school"]),
        new_extension_token=new_extension_token)


def _estimate_cost(usage_rows) -> float:
    total = 0.0
    for row in usage_rows:
        pin, pout = study_llm.PROVIDERS[row["provider"]]["price"]
        total += row["tokens_in"] / 1e6 * pin + row["tokens_out"] / 1e6 * pout
    return round(total, 2)


# --------------------------------------------------------------- AI tasks
def _make_usage_callback(user_id: int, task_name: str):
    """LLM 呼び出しごとに「利用履歴の記録」と「クレジットの引き落とし」を行う。

    study_llm 側が (provider, 入力トークン, 出力トークン) で呼んでくれるので、
    ここで実勢価格から USD を出して残高から引く。学生に見せる数字はこの USD。
    """
    def cb(provider, tin, tout):
        db.deduct_tokens(user_id, provider, tin, tout, task_name)
        db.charge_credit(user_id, _token_cost(provider, tin, tout),
                         f"{task_name} ({provider})")
    return cb


def _token_cost(provider: str, tokens_in: int, tokens_out: int) -> float:
    """トークン数 → USD。単価は study_agent/llm.py の PROVIDERS[...]["price"]。"""
    price_in, price_out = study_llm.PROVIDERS[provider]["price"]
    return tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out


def _user_routing(user_id: int) -> dict:
    """タスク種別 → 使うプロバイダ。

    クレジット方式ではプロバイダごとの上限が無いので、既定の振り分けを
    そのまま使う(実行してよいかどうかは db.has_credit() で別に判定する)。
    """
    return dict(DEFAULT_ROUTING)


@app.route("/task/notes", methods=["POST"])
@login_required
def task_notes():
    return _run_task("notes")


@app.route("/task/draft", methods=["POST"])
@login_required
def task_draft():
    return _run_task("draft")


@app.route("/task/quiz", methods=["POST"])
@login_required
def task_quiz():
    return _run_task("quiz")


def _run_task(kind: str):
    user_id = session["user_id"]
    if not db.has_credit(user_id):
        flash(t("flash.no_credit"))
        return redirect(url_for("plans"))

    f = request.files.get("file")
    if not f or not f.filename:
        flash(t("flash.file_missing"))
        return redirect(url_for("dashboard"))

    course_choice = request.form.get("course", "").strip()
    course_id = None
    if course_choice and course_choice != "__none__":
        course_id = db.add_course(user_id, course_choice)

    in_path = UPLOAD_DIR / f"{user_id}_{f.filename}"
    f.save(in_path)
    cb = _make_usage_callback(user_id, kind)
    routing = _user_routing(user_id)

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    base = Path(f.filename).stem
    ext = "docx" if kind in ("notes", "draft") else "md"
    out_filename = f"{user_id}_{stamp}_{kind}_{base}.{ext}"
    out_path = OUTPUT_DIR / out_filename

    # 生成物の言語は画面の言語に合わせる(英語で使っているなら英語のノート)
    out_lang = i18n.ai_output_lang()

    try:
        if kind == "notes":
            from study_agent.notes import make_notes
            out = make_notes(str(in_path), out_path=str(out_path), lang=out_lang,
                             api_keys=MASTER_KEYS, usage_callback=cb, routing=routing)
        elif kind == "draft":
            from study_agent.assignment import make_draft
            out = make_draft(str(in_path), out_path=str(out_path), lang=out_lang,
                             api_keys=MASTER_KEYS, usage_callback=cb, routing=routing)
        else:  # quiz
            from study_agent.quiz import answer_quiz
            text = answer_quiz(str(in_path), notes_dir=str(OUTPUT_DIR), lang=out_lang,
                               api_keys=MASTER_KEYS, usage_callback=cb,
                               routing=routing)
            out_path.write_text(text, encoding="utf-8")
            out = str(out_path)
    except Exception as e:  # noqa: BLE001
        flash(t("flash.error", message=e))
        return redirect(url_for("dashboard"))

    # 表示名は `Week {N}: {課題名}`。週は Canvas の表記が最優先(weeks.py)
    week_no = weeks.week_of(f.filename)
    db.add_note(user_id, course_id, kind, out_filename, f.filename,
                title=weeks.note_title(f.filename, week=week_no),
                week_no=week_no, term=weeks.term_of())
    return send_file(out, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, port=5050)