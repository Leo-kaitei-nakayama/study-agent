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

from flask import (Flask, flash, g, redirect, render_template, request,
                   send_file, session, url_for)

sys.path.insert(0, str(Path(__file__).parent.parent))  # study_agent パッケージを見える化

import crypto
import db
import i18n
import mailer
import payments
import school as school_info
import transcript as transcript_parser
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


@app.route("/notes")
@login_required
def notes_library():
    user_id = session["user_id"]
    courses = db.list_user_courses(user_id)
    counts = db.notes_count_by_course(user_id)
    uncategorized_count = counts.get(None, 0)
    return render_template("notes_library.html", courses=courses, counts=counts,
                          uncategorized_count=uncategorized_count)


@app.route("/notes/course/<int:course_id>")
@login_required
def notes_course(course_id):
    user_id = session["user_id"]
    course = db.get_course(user_id, course_id)
    if not course:
        flash(t("flash.course_missing"))
        return redirect(url_for("notes_library"))
    notes = db.list_notes_for_course(user_id, course_id)
    return render_template("notes_course.html", course=course, notes=notes)


@app.route("/notes/uncategorized")
@login_required
def notes_uncategorized():
    user_id = session["user_id"]
    notes = db.list_notes_for_course(user_id, None)
    return render_template("notes_course.html", course=None, notes=notes)


@app.route("/notes/download/<int:note_id>")
@login_required
def notes_download(note_id):
    user_id = session["user_id"]
    row = db.get_note(user_id, note_id)
    if not row:
        flash(t("flash.note_missing"))
        return redirect(url_for("notes_library"))
    return send_file(OUTPUT_DIR / row["filename"], as_attachment=True,
                     download_name=row["source_name"])


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

    course_id = db.add_course(user_id, course_name)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    filename = f"{user_id}_{stamp}_syllabus_{course_name}.md".replace(" ", "_")
    (OUTPUT_DIR / filename).write_text(text, encoding="utf-8")
    db.add_note(user_id, course_id, "syllabus", filename, f"{course_name} syllabus")
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
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    for entry in courses:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
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
            db.add_note(user_id, course_id, "syllabus", fn, f"{name} syllabus")
            saved_syllabi += 1

        assignments = entry.get("assignments") or []
        if assignments:
            lines = [f"# {name} — 課題一覧", ""]
            for a in assignments:
                if not isinstance(a, dict):
                    continue
                due = a.get("due_at") or "期限なし"
                lines.append(f"## {a.get('name', '(名称なし)')}")
                lines.append(f"- 期限: {due}")
                if a.get("points") is not None:
                    lines.append(f"- 配点: {a['points']}")
                desc = (a.get("description") or "").strip()
                if desc:
                    lines.append("")
                    lines.append(desc)
                lines.append("")
            fn = f"{user_id}_{stamp}_assignments_{course_id}.md"
            (OUTPUT_DIR / fn).write_text("\n".join(lines), encoding="utf-8")
            db.add_note(user_id, course_id, "assignments", fn,
                       f"{name} 課題一覧")
            saved_assignments += 1

    pending = db.list_pending_links(user_id, thin_only=True)
    return {"status": "ok", "courses": len(courses),
            "syllabi": saved_syllabi, "assignments": saved_assignments,
            "pending_external_syllabi": [
                {"url": p["url"], "label": p["label"],
                 "course": p["course_name"]} for p in pending]}


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
    course_id = db.add_course(user_id, course_name) if course_name else None

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

    course_id = db.add_course(user_id, course_name) if course_name else None

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", title)[:40] or "page"
    fn = f"{user_id}_{stamp}_{kind}_{safe}.md"
    body = f"# {title}\n\n出典: {url}\n\n---\n\n{text}"
    (OUTPUT_DIR / fn).write_text(body, encoding="utf-8")
    db.add_note(user_id, course_id, kind, fn, title)
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

    if course_name:
        course_id = db.add_course(user_id, course_name)
        db.add_note(user_id, course_id, "practice_answer",
                   f"_inline_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.md",
                   question_text[:60])
    return {"answer": answer}


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
    return render_template(
        "academic.html",
        school=sch,
        meta=db.get_transcript_meta(user_id),
        summary=transcript_parser.compute_gpa(rows, profile["school"], official),
        terms=transcript_parser.group_by_term(rows, profile["school"]),
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


@app.route("/academic/manual", methods=["POST"])
@login_required
def academic_manual():
    """HTML の解析に失敗したとき用に、履修を 1 件ずつ手入力で足す。"""
    user_id = session["user_id"]
    term = request.form.get("term", "").strip()
    code = request.form.get("code", "").strip().upper()
    title = request.form.get("title", "").strip()
    grade = request.form.get("grade", "").strip().upper()
    try:
        units = float(request.form.get("units", ""))
    except ValueError:
        flash(t("flash.units_numeric"))
        return redirect(url_for("academic"))

    if not (term and code and grade) or units <= 0:
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

    db.add_note(user_id, course_id, kind, out_filename, f.filename)
    return send_file(out, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, port=5050)