"""ブラウザ操作モジュール (browser-use + Playwright)。

- config.yaml の allowed_domains でアクセス先を制限できる(空なら制限なし)
- ガードレール: 提出・採点確定などの不可逆な操作はエージェントに禁止させる
- user_data_dir でブラウザプロファイル(Cookie含む)を保存・再利用する。
  一度手動で2段階認証を突破すれば、Canvasが「このデバイスを記憶」している間は
  次回以降ログイン不要で動く。
- このモジュールはローカル(Leoのラップトップ)での実行を前提にしている。
  headless: false で実ブラウザ画面が開くので、2FAが必要な場面ではLeo自身が
  その画面で直接コードを入力する。Renderなどの画面のないサーバー上では
  2FAを人間が突破できないため、この機能は使えない。
"""
import asyncio
from pathlib import Path

import yaml

_GUARDRAIL = (
    "\n\nIMPORTANT RULES:\n"
    "- Never click Submit / Turn in / 提出 / 送信 or any button that finalizes "
    "a graded submission. Stop and report instead.\n"
    "- Never make purchases or change account settings.\n"
    "- If a login page appears and no saved session works, STOP and tell the user "
    "to log in manually in this browser window (including any 2FA prompt), then "
    "ask them to say 'continue' once they're logged in."
)


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


async def run_browser_task(task: str, config_path: str = "config.yaml",
                           course: str | None = None) -> str:
    from browser_use import Agent, BrowserSession
    from browser_use.llm import ChatAnthropic

    from . import memory as mem

    cfg = load_config(config_path)
    allowed = cfg.get("allowed_domains") or None
    model = cfg.get("model", "claude-sonnet-4-6")
    user_data_dir = cfg.get("user_data_dir")  # 例: "./canvas_profile"

    # 保存済みログイン情報を sensitive_data として渡す。
    # browser-use はLLMにプレースホルダ名だけを見せ、実際の値はブラウザにのみ注入する。
    sensitive = {}
    cred_hint = []
    for site in mem.list_credential_sites():
        cred = mem.get_credential(site)
        if cred:
            u_key = f"{_slug(site)}_user"
            p_key = f"{_slug(site)}_pass"
            sensitive[u_key] = cred[0] or ""
            sensitive[p_key] = cred[1]
            cred_hint.append(f"- {site}: username={u_key}, password={p_key}")

    full_task = task
    if course:
        ctx = mem.course_context(course)
        if ctx:
            full_task = f"{ctx}\n\nTask: {task}"
    if cred_hint:
        full_task += ("\n\nUse these stored credentials if a login is needed "
                      "(refer to them by these placeholder names):\n"
                      + "\n".join(cred_hint))
    full_task += _GUARDRAIL

    session = BrowserSession(
        allowed_domains=allowed,      # 例: ["canvas.eee.uci.edu", "*.uci.edu"]
        headless=cfg.get("headless", False),
        user_data_dir=user_data_dir,  # Cookie保存先。Noneならプロファイル保存しない
        keep_alive=False,
    )
    agent = Agent(
        task=full_task,
        llm=ChatAnthropic(model=model),
        browser_session=session,
        sensitive_data=sensitive or None,
    )
    history = await agent.run(max_steps=cfg.get("max_steps", 40))
    return history.final_result() or "(結果なし)"


def _slug(site: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", site.lower()).strip("_")


def browse(task: str, config_path: str = "config.yaml",
           course: str | None = None) -> str:
    return asyncio.run(run_browser_task(task, config_path, course=course))


def fetch_canvas_syllabus(course_name: str, config_path: str = "config.yaml") -> str:
    """指定した科目のCanvasシラバスページから本文を抽出して返す。"""
    task = (
        f"Go to Canvas (canvas.eee.uci.edu). If not already logged in, log in "
        f"using the saved credentials if available, or wait for the user to log "
        f"in manually (including 2FA). Once logged in, find the course named "
        f"approximately '{course_name}' in the course list — it may not match "
        f"exactly, use your judgment to find the closest match. Open that "
        f"course's Syllabus page (usually in the left sidebar navigation). "
        f"Extract and return the FULL text content of the syllabus page, "
        f"including any assignment/grading policy tables. Do not summarize — "
        f"return the actual text as your final result."
    )
    return browse(task, config_path=config_path, course=course_name)