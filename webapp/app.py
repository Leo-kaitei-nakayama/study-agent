#!/usr/bin/env python3
"""Study Agent — Webアプリ (Flask)。

サインアップ→メール確認(モック)→ログイン→プラン購入(モック決済)→
本人のステータス画面(ダッシュボード)、という流れ。

運営者(あなた)のマスターAPIキーは環境変数から読む:
  ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY
学生は課金するだけで、鍵を自分で入力する必要はない。
"""
import os
import sys
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, flash, redirect, render_template, request,
                   send_file, session, url_for)

sys.path.insert(0, str(Path(__file__).parent.parent))  # study_agent パッケージを見える化

import db
import mailer
import payments
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
            flash("メールアドレスとユーザー名を入力してください")
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
        flash("コードが正しくないか、期限切れです")
    dev_code = request.args.get("dev_code")
    return render_template("verify.html", dev_code=dev_code)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = db.get_user_by_email(email)
        if not user:
            flash("そのメールアドレスのアカウントが見つかりません")
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
        flash("コードが正しくないか、期限切れです")
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
            flash("呼び方を入力してください")
            return render_template("onboarding.html")

        db.set_profile(user_id, preferred_name, major, school)
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
        flash("科目が見つかりません")
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
        flash("ファイルが見つかりません")
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
    if request.method == "POST":
        plan = request.form["plan"]
        ok = payments.charge(session["user_id"], plan, PLANS[plan]["price_usd"])
        if ok:
            db.set_plan(session["user_id"], plan)
            flash(f"{PLANS[plan]['label']} プランを有効化しました")
            return redirect(url_for("dashboard"))
        flash("決済に失敗しました")
    return render_template("plans.html", plans=PLANS, default=DEFAULT_PLAN)


# -------------------------------------------------------------- dashboard
@app.route("/dashboard")
@login_required
def dashboard():
    return _render_dashboard()


def _render_dashboard(new_extension_token: str | None = None):
    user = db.get_user(session["user_id"])
    profile = db.get_profile(session["user_id"])
    if not profile:
        return redirect(url_for("onboarding"))
    sub = db.get_subscription(session["user_id"])
    if not sub:
        return redirect(url_for("plans"))
    usage = db.usage_this_cycle(session["user_id"])
    cost = _estimate_cost(usage)
    courses = db.list_user_courses(session["user_id"])

    return render_template("dashboard.html", user=user, profile=profile, sub=sub,
                          plan=PLANS[sub["plan"]], usage_count=len(usage),
                          cost=cost, courses=courses,
                          new_extension_token=new_extension_token)


def _estimate_cost(usage_rows) -> float:
    total = 0.0
    for row in usage_rows:
        pin, pout = study_llm.PROVIDERS[row["provider"]]["price"]
        total += row["tokens_in"] / 1e6 * pin + row["tokens_out"] / 1e6 * pout
    return round(total, 2)


# --------------------------------------------------------------- AI tasks
def _make_usage_callback(user_id: int, task_name: str):
    def cb(provider, tin, tout):
        db.deduct_tokens(user_id, provider, tin, tout, task_name)
    return cb


def _user_routing(user_id: int) -> dict:
    sub = db.get_subscription(user_id)
    # 残トークンが尽きたプロバイダは既定ルーティングから外す(枯渇時は他へ回す)
    routing = dict(DEFAULT_ROUTING)
    for task, provider in list(routing.items()):
        if sub and sub[f"remaining_{provider}"] <= 0:
            for alt in ("openai", "claude"):  # DeepSeekは今は未使用
                if sub[f"remaining_{alt}"] > 0:
                    routing[task] = alt
                    break
    return routing


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
    f = request.files.get("file")
    if not f or not f.filename:
        flash("ファイルを選択してください")
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

    try:
        if kind == "notes":
            from study_agent.notes import make_notes
            out = make_notes(str(in_path), out_path=str(out_path),
                             api_keys=MASTER_KEYS, usage_callback=cb, routing=routing)
        elif kind == "draft":
            from study_agent.assignment import make_draft
            out = make_draft(str(in_path), out_path=str(out_path),
                             api_keys=MASTER_KEYS, usage_callback=cb, routing=routing)
        else:  # quiz
            from study_agent.quiz import answer_quiz
            text = answer_quiz(str(in_path), notes_dir=str(OUTPUT_DIR),
                               api_keys=MASTER_KEYS, usage_callback=cb,
                               routing=routing)
            out_path.write_text(text, encoding="utf-8")
            out = str(out_path)
    except Exception as e:  # noqa: BLE001
        flash(f"エラー: {e}")
        return redirect(url_for("dashboard"))

    db.add_note(user_id, course_id, kind, out_filename, f.filename)
    return send_file(out, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, port=5050)