# Study Agent Chrome 拡張機能

Canvas から科目・シラバス・課題を取り込み、いま見ているページについて
エージェントに質問し、下書きができたら知らせる。

**提出は一切しない。** できるのは下書きまでで、出すかどうかは必ず本人が決める。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `manifest.json` | 権限とエントリポイントの宣言 |
| `background.js` | 同期・スクリーンショット・通知。サーバーとのやり取りは全部ここ |
| `content.js` | Canvas のページ上でリンクを拾って報告するだけ(追加でページを開かない) |
| `popup.html` / `popup.js` | アイコンを押したときの小窓 |

## 権限について

| 権限 | なぜ必要か |
|---|---|
| `storage` | 接続トークンと設定を覚える |
| `alarms` | 定期同期(6時間)・通知の確認(30分)・週の始まり(毎週月曜) |
| `activeTab` | **アイコンを押した瞬間だけ**、そのタブを撮れるようにする |
| `notifications` | 下書きができたこと / 今週の課題を知らせる |
| `host_permissions: canvas.eee.uci.edu` | Canvas の公式 API を読む |

`<all_urls>` は要求していない。`activeTab` はユーザーがアイコンを押したときに
だけ有効になるので、勝手に画面を撮ることはできない。

## Canvas からの取り込み

学生はすでにブラウザで Canvas にログインしている。`host_permissions` に
Canvas を入れてあるので `fetch(..., {credentials: "include"})` にそのCookieが
付く。**APIトークンの発行も2FAも要らない。**

読むのは公式の REST API だけで、DOM を辿って画面を巡回することはしない:

| 取るもの | エンドポイント |
|---|---|
| 履修中の科目 | `GET /api/v1/courses?enrollment_state=active&include[]=term` |
| シラバス | `GET /api/v1/courses/{id}?include[]=syllabus_body` |
| 課題・ルーブリック | `GET /api/v1/courses/{id}/assignments` |
| 週ごとのモジュール | `GET /api/v1/courses/{id}/modules?include[]=items` |

### 初回と2回目以降

- **初回 (`fullCrawl`)** … 科目一覧・シラバス・課題・モジュールを全部取る。
  終わったら「科目名 → Canvas科目ID」の対応表を `chrome.storage.local` に
  覚える。
- **2回目以降 (`incrementalSync`)** … 覚えた対応表に直接あたる(科目一覧の
  再取得もシラバスページの再訪問もしない)。さらにサーバーに
  `assignment_state` を聞き、Canvas の `updated_at` と一致する課題は
  **送らない**。1件も変わっていない科目は POST 自体を省く。

対応表を作り直したいときは、小窓の「再取得」で忘れさせる。

## 週の始まり(毎週月曜)

月曜 00:05(端末の時計、週の境目は `weeks.py` と同じく月曜)に起きて、
差分同期をしてから `GET /api/extension/week` で今週の課題を取り、
まとめて1件だけ通知を出す。**下書きは作らない** — 何を手伝わせるかは
`/assignments` で本人が選ぶ。

## スクリーンショットで質問する

アイコン → 「C. Ask about this page」でモードを選び、ボタンを押す:

| モード | 何をするか |
|---|---|
| Explanation | 何を問われていて、どう考えるかを説明する |
| Answer | 解答の下書きを作る(**提出はしない**) |
| Other | 自由入力の指示に従う(例: この読み物を3行で要約して) |

**Canvas では撮らない。** Canvas は学生自身が操作すると決めてあるため、
`background.js` と サーバー側 (`app.py` の `BLOCKED_SCREENSHOT_HOSTS`) の
両方で弾いている。Perusall のように本文を取れないサイトで、読み物を要約したり
コメントを書いたりするために使う。

結果はその場に出るほか、ノートとしても保存されるので後から見返せる。

## 通知

30分ごとにサーバーへ「下書きができた課題」を聞きに行き、あればブラウザの通知を
出す。クリックするとその下書きが開く。一度知らせたものは印が付くので、
同じ通知が繰り返し出ることはない。通知が言うのは「下書きができた」ことだけで、
提出は行わない。
