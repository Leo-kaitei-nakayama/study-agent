# Study Agent

ノート作成・練習課題のドラフト作成・ブラウザ操作ができる、ラップトップで動くエージェント。

## できること

| 機能 | 内容 |
|---|---|
| `notes` | 教科書・スライド (pdf / docx / pptx / txt / md) → 構造化ノート (.docx) |
| `draft` | 練習課題 → 解答ドラフト+解説 (.docx)。**自動提出はしない** |
| `browse` | browser-use + Playwright で Web タスクを自然言語で実行 |
| GUI | `python gui.py` — デスクトップ常駐の小窓。初回起動でAPI選択・キー入力・クレジット設定 |

### 速度とAPIルーティング

- **ブラウザは `browse` のときだけ起動**します。`notes` / `draft` はブラウザを一切使わないので高速です。
- タスク内容を正規表現ヒューリスティックで瞬時に分類し(LLM呼び出しなし)、自動でAPIを振り分けます:
  - 数学・CS(数式、コード、証明、計算量など)→ **Claude**(精度重視)
  - 選択式問題(A) B) C) の選択肢を検出)→ **OpenAI (gpt-4o-mini)**(安価・十分)
  - 通常の思考・要約 → **DeepSeek**(安価)
- `--provider claude` などで固定も可能。振り分け先は `~/.study_agent/settings.json` の `routing` で変更できます。

### クレジット管理

各API呼び出しのトークン数から概算コスト(USD)を計算し、ローカル残高から差し引きます。残高・履歴は `~/.study_agent/` に保存され、GUIの「+ チャージ」で追加できます。**注意:** これはローカルの帳簿であり、実際の課金は各APIプロバイダのアカウントに発生します。他人に使わせて実際にお金を「チャージ」してもらうサービスにする場合は、決済(Stripe等)とサーバーが別途必要で、各プロバイダの利用規約(API転売の可否)の確認も必要です。

`.docx` はそのまま Google Drive にアップすれば Google Docs で開けます。

## セットアップ

```bash
cd study-agent
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env             # APIキーを記入
export ANTHROPIC_API_KEY=sk-ant-...   # または .env を読み込むツールを使用
```

## 使い方

```bash
# GUI(推奨)
python gui.py

# ノート作成
python agent.py notes textbook_ch3.pdf
python agent.py notes lecture5.pptx --style summary --lang English

# 課題ドラフト(解答+解説、DRAFT表示付き)
python agent.py draft practice_set2.pdf
python agent.py draft hw3.docx --extra "SQLで解くこと" --provider claude

# ブラウザタスク(config.yaml の allowed_domains 内のみ)
python agent.py browse "CanvasのToDoを一覧して、締切順に教えて"
```

## 設定 (config.yaml)

- `allowed_domains`: エージェントがアクセスできるサイトを制限(Canvas 等)。制限なしにするならリストを空に。
- `headless`: `false` だとブラウザ画面が見える(ログインが必要なとき便利)。

## 長期メモリ / 短期メモリ

保存場所は `~/.study_agent/memory/`。GUIの「🧠 メモリ」またはCLIで管理します。

**長期に持つもの:**

| 内容 | 保存先 | 備考 |
|---|---|---|
| ログイン情報(Canvas等) | **OSキーチェーン** | 平文にしない。ログイン時もAIに渡らない(browser-useの`sensitive_data`でプレースホルダ化) |
| 学期(クォーター/セメスター)の期間 | `terms.json` | 失効の基準になる |
| 科目ごとの「課題の出され方」(初日の方針) | `courses.json` | これを基に、実行時に全課題を漏れなく処理 |

**長期に持たないもの:** 日々のノート(ファイルとして別保存。クイズ時にその日の分を参照)、ブラウザ1タスク内の履歴。

**自動失効:** `cleanup` を実行すると、終了した学期に属する科目メモリを猶予期間(既定30日)後に削除します。`--delete-credentials` を付ければ、その科目のサイトのログイン情報も一緒に消せます。定期実行(cron/タスクスケジューラ)に登録しておくと自動で片付きます。

```bash
python agent.py term "Fall 2026" 2026-09-24 2026-12-13
python agent.py course "MGMT 109" "Fall 2026" "HWは毎週月曜Canvasに出る。締切は次の月曜。任意の練習クイズは出版社サイト" --sites canvas.eee.uci.edu publisher.example.com
python agent.py cred canvas.eee.uci.edu zhangkaicheng   # パスワードは非表示で入力
python agent.py quiz quiz3.pdf --notes-dir ./notes --course "MGMT 109"
python agent.py browse "MGMT 109の今週のHWを確認して" --course "MGMT 109"
python agent.py cleanup --grace-days 30
```

**クイズの解き方(指定どおり):** `quiz` はまず**その日に作ったノート**を根拠に回答し、根拠には (ノート由来) を付けます。ノートで足りない分だけ一般知識で補い、そこは (ノート外・要確認) と明示します。

## 安全設計

- **draft は必ず「DRAFT」バナー付き**で出力され、最後に「レビュー時のチェックポイント」が付く。成績・評価に関わる提出物は、自分で内容を確認・修正してから使うこと。
- **browse は提出・送信・購入などの不可逆操作を禁止**するルールをタスクに自動付加。
- **パスワードはOSキーチェーンにのみ保存**し、平文ファイルやAIプロンプトには一切載せない。キーチェーンが無い環境では暗号化ファイルにフォールバックするが、これはキーチェーンより弱い(鍵が同じ端末上にあるため)。可能な限りキーチェーンのある環境で使うこと。

## よくあるつまずき

- `browser-use` は API が変わりやすい。`browser.py` の import でエラーが出たら `pip install -U browser-use` して、公式 README の `Agent` / `BrowserSession` の書き方に合わせて数行直せば OK。
- スキャン画像だけの PDF はテキスト抽出できない(OCR が必要)。その場合は教えてくれれば OCR 対応を足せます。
