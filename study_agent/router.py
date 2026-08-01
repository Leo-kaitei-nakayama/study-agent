"""課題の下書きを作るときの交通整理。

`POST /api/study/generate-draft` から呼ばれる。やることは 3 つ:

  1. どこの課題文かを見分ける       … detect_context()
  2. その課題文を手に入れる         … resolve_prompt()
  3. 科目の文脈を足して LLM に渡す   … generate_draft()

--------------------------------------------------------------------------
課題文をどこから取るか
--------------------------------------------------------------------------
Canvas の中で完結している課題は API で取れている(assignments.description)。
問題は Canvas の外にあるもの:

  EMBEDDED_LTI      … Canvas の中に iframe で埋まっている (Gradescope 等)
  EXTERNAL_PLATFORM … 別サイトそのもの (zyBooks / Pearson / PrairieLearn)

これらは **拡張機能が読んだページ本文を送ってもらう** のが正しい。理由:

  - 学生のブラウザはすでにそのサイトにログインしている。サーバーから
    Playwright で取りに行くと、ログイン画面で止まる
  - 突破しようとすると保存したパスワードを使うことになり、2FA が出れば
    画面の無いサーバー(Render)では誰も応じられない
  - そもそも学生が今見ているページなのだから、読み直す必要がない

そのため既定は「拡張機能が送ってきた page_text を使う」。
サーバー側の Playwright (browser.py) は **明示的に有効にしたときだけ**
使う逃げ道として残してある(手元の CLI 実行を想定。browser.py の冒頭コメント
参照)。有効でないときは黙って失敗せず、「本文を送ってほしい」と返す。

--------------------------------------------------------------------------
やらないこと
--------------------------------------------------------------------------
**提出しない。** ここが作るのは下書きだけで、送信ボタンを探す処理は
一切書かない。生成物の先頭には必ず DRAFT の断り書きが入る。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from study_agent import llm

# ------------------------------------------------------------------ 文脈
CANVAS_NATIVE = "CANVAS_NATIVE"
EMBEDDED_LTI = "EMBEDDED_LTI"
EXTERNAL_PLATFORM = "EXTERNAL_PLATFORM"
CONTEXTS = (CANVAS_NATIVE, EMBEDDED_LTI, EXTERNAL_PLATFORM)

# Canvas そのもの。instructure.com は Canvas のホスティング先。
CANVAS_HOSTS = ("canvas.eee.uci.edu", "instructure.com")

# Canvas の外にある、よく使う課題サイト。ここに無くても外部として扱うので、
# この表は「名前を出して分かりやすくする」ためのもの。
KNOWN_PLATFORMS = {
    "gradescope.com": "Gradescope",
    "zybooks.com": "zyBooks",
    "pearson.com": "Pearson",
    "mylab.pearson.com": "MyLab",
    "prairielearn.com": "PrairieLearn",
    "prairielearn.org": "PrairieLearn",
    "perusall.com": "Perusall",
    "edstem.org": "Ed",
    "piazza.com": "Piazza",
    "webassign.net": "WebAssign",
    "mheducation.com": "McGraw Hill",
    "cengage.com": "Cengage",
    "wiley.com": "Wiley",
}

# サーバー側から Playwright を使ってよいか。既定は無効。
# Render のような画面の無い環境では 2FA を突破できないため、有効にしても
# ログイン待ちで止まるだけ。手元で CLI から動かすときにだけ 1 にする。
ALLOW_SERVER_BROWSER = os.getenv("ALLOW_SERVER_BROWSER", "") == "1"

# LLM に渡す課題文の上限。長いページ全部を投げるとトークン代が跳ねる。
MAX_PROMPT_CHARS = 12_000


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def platform_name(url: str) -> str:
    """URL からサイト名を推測する。分からなければホスト名をそのまま返す。"""
    host = _host(url)
    if not host:
        return ""
    for domain, label in KNOWN_PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host


def detect_context(url: str, in_iframe: bool = False) -> str:
    """その URL がどの種類の課題かを返す。

    in_iframe は拡張機能が教えてくれる(`window.top !== window.self`)。
    Canvas の中に埋まった Gradescope は、URL は gradescope.com なのに
    見た目は Canvas の中 — これを EMBEDDED_LTI として分けておくと、
    あとで「Canvas に戻れ」と案内できる。
    """
    host = _host(url)
    if not host:
        return CANVAS_NATIVE
    is_canvas = any(host == h or host.endswith("." + h) for h in CANVAS_HOSTS)
    if is_canvas:
        # Canvas 上の LTI 起動 URL。中身は外部サイトが描く。
        if re.search(r"/external_tools?/|/lti/|/modules/items/", url):
            return EMBEDDED_LTI
        return CANVAS_NATIVE
    return EMBEDDED_LTI if in_iframe else EXTERNAL_PLATFORM


# ------------------------------------------------------------- 課題文の入手
class PromptUnavailable(Exception):
    """課題文が手に入らなかった。どうすれば取れるかを reason に入れる。"""

    def __init__(self, message: str, reason: str = "no_text"):
        super().__init__(message)
        self.reason = reason


def resolve_prompt(payload: dict, stored_description: str = "") -> tuple[str, str]:
    """課題文と、その出どころを返す。

    優先順位:
      1. 拡張機能が読んだページ本文 (payload["page_text"])
         … 学生が今見ているページそのもの。いちばん確実
      2. 同期で取り込んである説明文 (stored_description)
         … Canvas 内で完結している課題はこれで足りる
      3. サーバー側 Playwright
         … ALLOW_SERVER_BROWSER=1 のときだけ。既定では使わない
    """
    page_text = _clean(payload.get("page_text") or "")
    if page_text:
        return page_text[:MAX_PROMPT_CHARS], "extension"

    stored = _clean(stored_description or "")
    context = payload.get("context") or CANVAS_NATIVE
    if stored and context == CANVAS_NATIVE:
        return stored[:MAX_PROMPT_CHARS], "canvas_api"

    url = (payload.get("url") or "").strip()
    if url and ALLOW_SERVER_BROWSER:
        text = _fetch_with_browser(url)
        if text:
            return text[:MAX_PROMPT_CHARS], "server_browser"

    if stored:                       # 外部でも、無いよりは同期済みの説明文を使う
        return stored[:MAX_PROMPT_CHARS], "canvas_api"

    site = platform_name(url) or "that page"
    raise PromptUnavailable(
        f"Could not read the assignment text from {site}. Open the assignment "
        "in your browser and press the button there, so the extension can read "
        "the page it is already signed in to.",
        reason="needs_page_text")


def _fetch_with_browser(url: str) -> str:
    """手元で動かしているときだけの逃げ道。Render では使えない。

    browser.py は実ブラウザを開く前提で書いてある(2FA を人が突破するため)。
    画面の無いサーバーでは必ずログイン待ちで止まるので、既定で無効。
    """
    try:
        from study_agent import browser
    except ImportError:
        return ""
    try:
        return browser.browse(
            f"Open {url} and return the full assignment instructions as plain "
            "text. Do not click any submit or save button.") or ""
    except Exception:      # noqa: BLE001 — 取れなければ「取れなかった」でよい
        return ""


def _clean(text: str) -> str:
    """余分な空白を落とす(同じ内容で毎回違うトークン数にならないように)。"""
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


# ------------------------------------------------------------- 下書きを作る
DRAFT_BANNER = (
    "> DRAFT — written by Study Agent from the assignment text. "
    "Read it, change what you disagree with, and submit it yourself.\n\n")

# 種類ごとの書き方。webapp の _assignment_kind() が付ける種類と対応。
_STYLE = {
    "quiz": "Answer each question and show the reasoning in one or two lines, "
            "so the student can check it rather than trust it.",
    "short": "Write a short response of a few sentences. Keep the student's own "
             "voice plain and direct rather than florid.",
    "code": "Write reference code with comments explaining each step, then list "
            "how to compile and test it.",
    "other": "Lay out what the task asks for, then draft a response to it.",
}


def build_context(course_name: str, strategy: dict | None,
                  assignment_name: str, prompt_text: str,
                  rubric: str = "", notes: str = "") -> str:
    """LLM に渡す本文を組み立てる。順番は「何の授業か → 何の課題か → 材料」。"""
    parts = [f"Course: {course_name or '(unknown)'}"]

    summary = (strategy or {}).get("summary") if strategy else ""
    if summary:
        # ここが planner.py の成果。的外れな言語で書き始めるのを防ぐ。
        parts.append(f"This course uses: {summary}")

    parts.append(f"Assignment: {assignment_name or '(untitled)'}")
    if rubric.strip():
        parts.append("How it is graded:\n" + rubric.strip()[:2000])
    if notes.strip():
        parts.append("The student's notes for this week:\n" + notes.strip()[:4000])

    parts.append("Assignment text:\n" + prompt_text)
    return "\n\n".join(parts)


def generate_draft(course_name: str, assignment_name: str, prompt_text: str,
                   kind: str = "other", strategy: dict | None = None,
                   rubric: str = "", notes: str = "", lang: str = "English",
                   api_keys: dict | None = None, usage_callback=None,
                   routing: dict | None = None) -> str:
    """下書きを 1 件作って返す。**提出はしない。**

    先頭に DRAFT の断り書きを必ず付ける。生成物をそのまま貼っても、
    それが下書きであることが読み手に分かるようにするため。
    """
    style = _STYLE.get(kind, _STYLE["other"])
    system = (
        "You are helping a university student with their coursework. "
        + style +
        " Work only from the assignment text you are given; if it is missing "
        "something you need, say what is missing instead of inventing it. "
        "Never claim the work has been submitted — the student submits it. "
        f"Write in {lang}.")

    body = build_context(course_name, strategy, assignment_name,
                         prompt_text, rubric, notes)
    answer = llm.complete(system, body, max_tokens=4096, api_keys=api_keys,
                          usage_callback=usage_callback, routing=routing)
    return DRAFT_BANNER + answer.strip() + "\n"


def route(payload: dict, *, course_name: str = "", stored_description: str = "",
          kind: str = "other", strategy: dict | None = None, rubric: str = "",
          notes: str = "", lang: str = "English", api_keys: dict | None = None,
          usage_callback=None, routing: dict | None = None) -> dict:
    """入口。文脈を見分け、課題文を取り、下書きを返すところまで。

    返り値: {"draft", "context", "source", "platform"}
    課題文が取れなければ PromptUnavailable を投げる(呼び出し側が 422 で返す)。
    """
    url = (payload.get("url") or "").strip()
    context = payload.get("context")
    if context not in CONTEXTS:
        context = detect_context(url, bool(payload.get("in_iframe")))
    payload = {**payload, "context": context}

    prompt_text, source = resolve_prompt(payload, stored_description)
    draft = generate_draft(
        course_name, payload.get("assignment_name") or "", prompt_text,
        kind=kind, strategy=strategy, rubric=rubric, notes=notes, lang=lang,
        api_keys=api_keys, usage_callback=usage_callback, routing=routing)

    return {"draft": draft, "context": context, "source": source,
            "platform": platform_name(url)}
