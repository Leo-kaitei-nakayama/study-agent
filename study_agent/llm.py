"""マルチプロバイダLLMルーター。

- タスク内容をヒューリスティック(API呼び出しなし=高速・無料)で分類し、
  math_cs → Claude / multiple_choice → OpenAI / general → DeepSeek にルーティング
- provider="claude" 等で明示指定も可能。"auto" で自動判定
- 使用トークンからコストを概算し、ローカル残高(クレジット)から差し引く
"""
import os
import re

from . import settings as st

# 価格は 100万トークンあたりUSD (input, output)。変わったらここを更新。
PROVIDERS = {
    "claude": {
        "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        "env_key": "ANTHROPIC_API_KEY",
        "price": (3.00, 15.00),
    },
    "openai": {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "env_key": "OPENAI_API_KEY",
        "base_url": None,
        "price": (0.15, 0.60),
    },
    "deepseek": {
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "price": (0.27, 1.10),
    },
}

_MCQ = re.compile(r"(^|\n)\s*[A-Ea-e][).、.]\s+\S", re.M)
_MATH_CS = re.compile(
    r"(\d+\s*[+\-*/^=<>]\s*\d+|\\frac|\\sum|\bintegral\b|\bderivative\b|"
    r"\bproof\b|証明|微分|積分|行列|\balgorithm\b|\bBig-?O\b|O\(n|"
    r"\bdef \w+\(|\bclass \w+|\bSELECT\b.+\bFROM\b|```|#include|\bSQL\b|"
    r"\brecursion\b|計算量|アルゴリズム)",
    re.I | re.S,
)


def classify(text: str) -> str:
    """タスク種別を高速に判定(LLMは使わない)。"""
    sample = text[:6000]
    mcq_hits = len(_MCQ.findall(sample))
    if _MATH_CS.search(sample):
        return "math_cs"
    if mcq_hits >= 3:  # A) B) C) の選択肢が複数 → 選択式問題
        return "multiple_choice"
    return "general"


def pick_provider(text: str, override: str = "auto") -> tuple[str, str]:
    """(provider名, 判定されたタスク種別) を返す。"""
    task = classify(text)
    if override and override != "auto":
        return override, task
    s = st.load()
    return s["routing"].get(task, s["default_provider"]), task


def _api_key(name: str, s: dict) -> str:
    key = (s["api_keys"].get(name) or "").strip() or os.getenv(
        PROVIDERS[name]["env_key"], "")
    if not key:
        raise RuntimeError(
            f"{name} のAPIキーが未設定です。GUIの設定画面か "
            f"環境変数 {PROVIDERS[name]['env_key']} で設定してください。")
    return key


def call_provider(name: str, system: str, user: str, max_tokens: int,
                  api_key: str) -> tuple[str, int, int]:
    """1回のAPI呼び出しを実行し (text, tokens_in, tokens_out) を返す。"""
    cfg = PROVIDERS[name]
    if name == "claude":
        from anthropic import Anthropic
        resp = Anthropic(api_key=api_key).messages.create(
            model=cfg["model"], max_tokens=max_tokens,
            system=system, messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    from openai import OpenAI  # openai / deepseek (OpenAI互換API)
    client = OpenAI(api_key=api_key, base_url=cfg.get("base_url"))
    resp = client.chat.completions.create(
        model=cfg["model"], max_tokens=max_tokens,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}])
    text = resp.choices[0].message.content or ""
    tin = resp.usage.prompt_tokens if resp.usage else 0
    tout = resp.usage.completion_tokens if resp.usage else 0
    return text, tin, tout


def complete(system: str, user: str, max_tokens: int = 4096,
             provider: str = "auto", quiet: bool = False,
             api_keys: dict | None = None, usage_callback=None,
             routing: dict | None = None) -> str:
    """LLM補完を実行する。

    api_keys / usage_callback を渡すとローカル設定(settings.py)を使わない。
    これにより、個人用CLI/GUIと、複数ユーザー向けWebアプリの両方から
    このルーターを共有できる(Webアプリはユーザーごとのトークン残高を
    usage_callback 経由で管理する)。
    """
    s = None if api_keys is not None else st.load()
    if s is not None and s.get("credits_remaining", 0) <= 0:
        raise RuntimeError("クレジット残高がありません。チャージしてください。")

    task = classify(user)
    if provider and provider != "auto":
        name = provider
    elif routing is not None:
        name = routing.get(task, next(iter(routing.values())))
    else:
        name = s["routing"].get(task, s["default_provider"])

    key = (api_keys or {}).get(name) or (_api_key(name, s) if s else None)
    if not key:
        raise RuntimeError(f"{name} のAPIキーが設定されていません。")

    text, tin, tout = call_provider(name, system, user, max_tokens, key)

    if usage_callback:
        usage_callback(name, tin, tout)
    else:
        cfg = PROVIDERS[name]
        pin, pout = cfg["price"]
        cost = tin / 1e6 * pin + tout / 1e6 * pout
        balance = st.record_usage(name, cfg["model"], tin, tout, cost)
        if not quiet:
            print(f"  [router] task={task} provider={name} "
                  f"cost=${cost:.4f} 残高=${balance:.2f}")
    return text
