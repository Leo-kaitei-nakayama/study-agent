"""外部サイトのパスワードを暗号化 / 復号するヘルパー。

■ このファイルの役割
ユーザーが「授業で使うサイト」(Perusall / 出版社サイト / Zybooks など)の
ログイン情報を登録すると、エージェントが後で自動ログインするために
パスワードを保管する必要がある。これを Supabase(Postgres)に平文で置くと、
DB のサービスキーが漏れた瞬間に全ユーザーのアカウントが奪われる。
そこで「アプリ層で暗号化してから DB に入れる」ための関数をここにまとめる。

■ 方式
Fernet(AES-128-CBC + HMAC-SHA256)。cryptography パッケージの標準機能で、
改ざん検知込み。暗号文は URL-safe base64 の文字列なので TEXT 列にそのまま入る。

■ 鍵の置き場所
環境変数 CREDENTIAL_KEY。生成方法:

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

DB とは別の場所(Render の Environment など)に置くこと。DB と鍵が同じ場所に
あると分けた意味がない。**鍵を失うと保存済みパスワードは復号できない**ので、
その場合はユーザーに再入力してもらう(復旧手段は用意しない)。

■ 呼び出し側の約束
- decrypt() は「使う直前」にだけ呼ぶ。復号した平文をログや LLM プロンプトに
  絶対に載せない(browser-use に渡すときは sensitive_data のプレースホルダ経由)。
- 鍵が未設定の環境では is_configured() が False を返すので、UI 側で
  「登録機能は使えません」と出して保存させない。
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def is_configured() -> bool:
    """CREDENTIAL_KEY が設定され、鍵として妥当かどうか。

    UI 側で「パスワード登録フォームを出してよいか」の判定に使う。
    """
    return _load() is not None


def _load() -> Fernet | None:
    """Fernet インスタンスを一度だけ作って使い回す。鍵が無ければ None。"""
    global _fernet
    if _fernet is None:
        raw = os.getenv("CREDENTIAL_KEY", "").strip()
        if not raw:
            return None
        try:
            _fernet = Fernet(raw.encode())
        except (ValueError, TypeError):
            # 鍵の形式が不正(長さ違い / base64 でない)。設定ミスとして扱う。
            return None
    return _fernet


def encrypt(plaintext: str) -> str:
    """平文パスワード → DB に保存する暗号文字列。

    鍵が未設定なら RuntimeError。呼ぶ前に is_configured() で確認すること。
    """
    f = _load()
    if f is None:
        raise RuntimeError(
            "CREDENTIAL_KEY が設定されていません。パスワードは保存できません。")
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    """DB の暗号文字列 → 平文パスワード。

    復号できない場合(鍵を入れ替えた / データが壊れている)は None を返す。
    呼び出し側は「再入力してください」と案内する。
    """
    f = _load()
    if f is None:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        return None
