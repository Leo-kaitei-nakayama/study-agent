"""メール送信。

現状: モック。実際には送らず、ローカルログに書き出す + 開発モードでは
verifyページに直接コードを表示する(email/決済は後で接続、という指定のため)。

本物のメール送信に切り替えるとき: この関数の中身だけを smtplib や
SendGrid/Mailgun 等のAPI呼び出しに置き換えれば良い。呼び出し側(app.py)は
変更不要。
"""
from datetime import datetime
from pathlib import Path

OUTBOX = Path(__file__).parent / "instance" / "mock_outbox.log"

# TODO(本番接続時): ここを実際のSMTP/メールAPI呼び出しに置き換える。
# 例: smtplib.SMTP + starttls、または SendGrid/Mailgun のREST API。
DEV_MODE = True


def send_verification_code(email: str, code: str, purpose: str) -> None:
    OUTBOX.parent.mkdir(exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.utcnow().isoformat()}] to={email} "
                f"purpose={purpose} code={code}\n")
