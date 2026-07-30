"""決済処理。

現状: モック。カード情報は一切扱わず、「購入」ボタンで即座に成功したものとして
プランを有効化する(email/決済は後で接続、という指定のため)。

本物の決済に切り替えるとき: charge() の中身を Stripe Checkout Session の
作成 + webhook でのプラン確定に置き換える。呼び出し側(app.py)の
「プラン選択→charge()→set_plan()」という流れ自体は変えなくてよい設計にしてある。
"""
from datetime import datetime
from pathlib import Path

LEDGER = Path(__file__).parent / "instance" / "mock_payments.log"

# TODO(本番接続時): ここを Stripe Checkout / PaymentIntent に置き換える。
# 本物の決済ではユーザーのカード情報を自前サーバーで扱わないこと(Stripe Elements等を使う)。


def charge(user_id: int, plan_name: str, price_usd: float) -> bool:
    """モック課金。常に成功する。戻り値は成否。"""
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.utcnow().isoformat()}] user={user_id} "
                f"plan={plan_name} amount_usd={price_usd} status=mock_success\n")
    return True
