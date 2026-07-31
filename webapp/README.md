# Study Agent — Webアプリ (アカウント・クレジット付き)

サインアップ → メール確認(モック) → ログイン → クレジットのチャージ(モック決済) →
ステータス画面(ダッシュボード)、という流れの複数ユーザー対応版。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `app.py` | Flask のルーティング全部。画面とAPIの入口 |
| `db.py` | Supabase (Postgres) への読み書き。SQL はここだけ |
| `crypto.py` | 外部サイトのパスワードを暗号化 / 復号 (Fernet) |
| `school.py` | 学校ごとの定数(UCI: クォーター制 / 卒業180単位 / AntAlmanac) |
| `transcript.py` | Unofficial Transcript (HTML) の解析と GPA 計算 |
| `plans.py` | チャージパックの定義 |
| `mailer.py` | 確認コードの送信(今はモック) |
| `payments.py` | 決済(今はモック) |

## 実行方法

```bash
cd webapp
pip install -r ../requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # 運営者(あなた)のマスターキー
export OPENAI_API_KEY=sk-...
export DEEPSEEK_API_KEY=...
export DATABASE_URL=postgresql://...     # Supabase の Connection string
export FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")
export CREDENTIAL_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
python3 app.py
```

http://127.0.0.1:5050 を開く。

テーブルは起動時に `db.init_db()` が `CREATE TABLE IF NOT EXISTS` で作るので、
Supabase 側で SQL を流す必要はない。

### CREDENTIAL_KEY について

「連携サービス」に登録された外部サイトのパスワードを暗号化する鍵。
**DATABASE_URL とは別の場所に置くこと**(同じ場所にあると分けた意味がない)。
この鍵を失うと保存済みパスワードは復号できなくなり、ユーザーに再入力して
もらうことになる。未設定の場合、パスワード登録フォームは無効化される。

## お金の流れ(クレジット方式)

学生は「プラン」= チャージパックを買う。買った額は **USD のクレジット残高** に
なり、AI を呼ぶたびに実際の概算コスト(トークン数 × 単価)が差し引かれる。
画面に出すのは残高だけで、プロバイダごとの残トークン数は表示しない。
実際の API 呼び出しは運営者のマスターキーで行われる。

残高が 0 以下になるとタスクの実行がブロックされ、チャージ画面へ誘導される。

## 学業 / GPA ページ

Student Access の「Unofficial Transcript」を ⌘S / Ctrl+S で保存した `.html` を
アップロードすると、履修科目・単位・成績を取り込んで累積 GPA と卒業単位
(UCI は 180)までの進捗を出す。AntAlmanac が読むのと同じファイル。

解析は特定のクラス名や id に依存しない「ゆるい」実装(`transcript.py` の
冒頭コメント参照)。それでも読めなかったときは手入力で 1 件ずつ足せる。

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
