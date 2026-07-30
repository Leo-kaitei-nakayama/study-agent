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
                  "general": "deepseek"}


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
    user = db.get_user(session["user_id"])
    sub = db.get_subscription(session["user_id"])
    if not sub:
        return redirect(url_for("plans"))
    usage = db.usage_this_cycle(session["user_id"])
    cost = _estimate_cost(usage)

    from study_agent import memory as mem
    courses = mem.list_courses()

    return render_template("dashboard.html", user=user, sub=sub,
                          plan=PLANS[sub["plan"]], usage_count=len(usage),
                          cost=cost, courses=courses)


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
            for alt in ("deepseek", "openai", "claude"):
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

    in_path = UPLOAD_DIR / f"{user_id}_{f.filename}"
    f.save(in_path)
    cb = _make_usage_callback(user_id, kind)
    routing = _user_routing(user_id)

    try:
        if kind == "notes":
            from study_agent.notes import make_notes
            out = make_notes(str(in_path),
                             out_path=str(OUTPUT_DIR / f"{user_id}_notes.docx"),
                             api_keys=MASTER_KEYS, usage_callback=cb, routing=routing)
        elif kind == "draft":
            from study_agent.assignment import make_draft
            out = make_draft(str(in_path),
                             out_path=str(OUTPUT_DIR / f"{user_id}_draft.docx"),
                             api_keys=MASTER_KEYS, usage_callback=cb, routing=routing)
        else:  # quiz
            from study_agent.quiz import answer_quiz
            text = answer_quiz(str(in_path), notes_dir=str(OUTPUT_DIR),
                               api_keys=MASTER_KEYS, usage_callback=cb,
                               routing=routing)
            out_path = OUTPUT_DIR / f"{user_id}_quiz_answer.md"
            out_path.write_text(text, encoding="utf-8")
            out = str(out_path)
    except Exception as e:  # noqa: BLE001
        flash(f"エラー: {e}")
        return redirect(url_for("dashboard"))

    return send_file(out, as_attachment=True)


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5050)
