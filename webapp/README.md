# Study Agent — Webアプリ (アカウント・プラン付き)

サインアップ → メール確認(モック) → ログイン → プラン購入(モック決済) →
ステータス画面(ダッシュボード)、という流れの複数ユーザー対応版。

## 実行方法

```bash
cd webapp
pip install -r ../requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # 運営者(あなた)のマスターキー
export OPENAI_API_KEY=sk-...
export DEEPSEEK_API_KEY=...
export FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")
python3 app.py
```

http://127.0.0.1:5050 を開く。

## 今モックになっている部分(指定どおり)

- **メール送信**: `mailer.py` — 実際には送らず、コードを `instance/mock_outbox.log` に記録し、
  確認画面に直接表示する(開発モード表示)。本番接続時は `send_verification_code()` の中身だけ
  SMTP/SendGrid等に差し替えればよい。
- **決済**: `payments.py` — 「購入」を押すと即座に成功したものとしてプランを有効化する。
  `instance/mock_payments.log` に記録される。本番接続時は `charge()` を Stripe Checkout に差し替える。

## お金とトークンの流れ

学生はプラン(Basic/Standard/Pro、`plans.py`)を購入します。実際のAPI呼び出しは
**運営者(あなた)のマスターAPIキー**(環境変数)で行われ、学生ごとに「今サイクルの
残トークン(Claude/ChatGPT/DeepSeekそれぞれ)」をSQLiteで管理します。プランで買った
分を使い切ったら、そのAPIへのルーティングは自動で他の(残っている)APIに回されます。

## データ

`instance/study_agent.db` (SQLite)。ユーザー・確認コード・サブスクリプション・利用履歴。
`instance/uploads/` `instance/outputs/` は課題ファイルと生成物の一時置き場です。

## まだ手動でやること

- 本番のメール送信・決済の接続(上記参照)
- HTTPS化(本番では `debug=True` を外し、gunicorn等の背後に置く)
- ノート/課題ドラフト/クイズ以外に、ブラウザ操作(`browse`)もWeb UIに繋ぐなら
  非同期実行(タスクキュー)にした方がよい — 現状はCLI (`agent.py browse`) からのみ利用可能
