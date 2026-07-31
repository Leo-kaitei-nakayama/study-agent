"""プラン(= クレジットのチャージパック)の定義。

■ 考え方
学生は「プラン」を買う。買った金額はそのまま **USD のクレジット残高** になり、
AI を呼ぶたびに実際の概算コストが差し引かれる。画面に出すのは残高(ドル)で、
プロバイダごとの残トークン数は出さない(学生には意味のない数字なので)。

  price_usd   … 請求額
  credit_usd  … 残高に入る額。price_usd より多い = まとめ買いの割引分。
  monthly_tokens … 旧トークン方式の名残。subscriptions テーブルの互換のために
                   残しているだけで、利用可否の判定には使っていない
                   (判定は db.has_credit() = 残高が正かどうか)。
"""

PLANS = {
    "basic": {
        "label": "Basic",
        "price_usd": 5,
        "credit_usd": 5.00,
        "browse_allowed": False,
        "blurb": "まずは試す。ノート作成 100 回ぶんくらい。",
    },
    "standard": {
        "label": "Standard",
        "price_usd": 15,
        "credit_usd": 16.50,      # +10% ボーナス
        "browse_allowed": True,
        "blurb": "毎週の課題ドラフト・クイズ支援まで。+10% ボーナス。",
    },
    "pro": {
        "label": "Pro",
        "price_usd": 35,
        "credit_usd": 42.00,      # +20% ボーナス
        "browse_allowed": True,
        "blurb": "Claude 優先で高精度。使い切らない量。+20% ボーナス。",
    },
}

DEFAULT_PLAN = "standard"

# 旧 subscriptions テーブルが NOT NULL を要求するための固定値。
# クレジット方式に移行したので、この数値で利用が止まることはない。
LEGACY_TOKEN_ALLOWANCE = {"claude": 0, "openai": 0, "deepseek": 0}
