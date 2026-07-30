"""プランごとの月間トークン付与量。

方針: 学生は「プラン(=月額)」を買う。実際のAPI呼び出しは運営側(あなた)の
マスターAPIキーで行われ、学生ごとの残トークンをここで管理して差し引く。
"""

PLANS = {
    "basic": {
        "label": "Basic",
        "price_usd": 5,
        "monthly_tokens": {"claude": 20_000, "openai": 200_000, "deepseek": 300_000},
        "browse_allowed": False,
        "blurb": "DeepSeek中心・ノート作成メイン",
    },
    "standard": {
        "label": "Standard",
        "price_usd": 15,
        "monthly_tokens": {"claude": 80_000, "openai": 400_000, "deepseek": 600_000},
        "browse_allowed": True,
        "blurb": "全API・ブラウザ操作・クイズ支援込み",
    },
    "pro": {
        "label": "Pro",
        "price_usd": 35,
        "monthly_tokens": {"claude": 300_000, "openai": 800_000, "deepseek": 1_000_000},
        "browse_allowed": True,
        "blurb": "Claude優先で高精度・上限大",
    },
}

DEFAULT_PLAN = "standard"
