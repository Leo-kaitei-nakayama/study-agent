"""ブラウザ操作モジュール (browser-use + Playwright)。

- config.yaml の allowed_domains でアクセス先を制限できる(空なら制限なし)
- ガードレール: 提出・採点確定などの不可逆な操作はエージェントに禁止させる
"""
import asyncio
from pathlib import Path

import yaml

_GUARDRAIL = (
    "\n\nIMPORTANT RULES:\n"
    "- Never click Submit / Turn in / 提出 / 送信 or any button that finalizes "
    "a graded submission. Stop and report instead.\n"
    "- Never make purchases or change account settings.\n"
    "- If login is required and you are not already logged in, stop and ask the user."
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
