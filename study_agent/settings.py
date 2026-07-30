"""ユーザー設定と使用量(クレジット)の管理。~/.study_agent/ に保存。"""
import json
import time
from pathlib import Path

DIR = Path.home() / ".study_agent"
SETTINGS_FILE = DIR / "settings.json"
USAGE_FILE = DIR / "usage.jsonl"

DEFAULTS = {
    "default_provider": "claude",       # 自動ルーティングで判定できない時のフォールバック
    "routing": {                        # タスク種別 → プロバイダ
        "math_cs": "claude",
        "multiple_choice": "openai",
        "general": "deepseek",
    },
    "api_keys": {"claude": "", "openai": "", "deepseek": ""},
    "credits_remaining": 5.00,          # USD換算のローカル残高(こちら側で「チャージ」する分)
    "setup_done": False,
}


def load() -> dict:
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        merged = {**DEFAULTS, **data}
        merged["api_keys"] = {**DEFAULTS["api_keys"], **data.get("api_keys", {})}
        merged["routing"] = {**DEFAULTS["routing"], **data.get("routing", {})}
        return merged
    return dict(DEFAULTS)


def save(settings: dict) -> None:
    DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        SETTINGS_FILE.chmod(0o600)  # APIキーを含むので本人のみ読み取り可
    except OSError:
        pass


def first_run_needed() -> bool:
    return not load().get("setup_done", False)


def record_usage(provider: str, model: str, tokens_in: int, tokens_out: int,
                 cost: float) -> float:
    """使用量を記録し、残高から差し引いて返す。"""
    DIR.mkdir(exist_ok=True)
    with USAGE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider, "model": model,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": round(cost, 6),
        }) + "\n")
    s = load()
    s["credits_remaining"] = round(s.get("credits_remaining", 0) - cost, 6)
    save(s)
    return s["credits_remaining"]


def add_credits(amount: float) -> float:
    s = load()
    s["credits_remaining"] = round(s.get("credits_remaining", 0) + amount, 6)
    save(s)
    return s["credits_remaining"]
