"""エージェントの長期メモリ。

保存内容(長期):
- terms      : クォーター/セメスターの期間。これを基準に古いメモリを失効させる
- courses    : 科目ごとの「課題の出され方(初日の方針)」+ 使うサイト + 所属term
- credentials: Canvas等のログイン情報。**OSキーチェーン**に保存(平文にしない)

保存しないもの(短期):
- 日々のノート(ファイルとして別途保存済み。クイズ時にその日の分を参照する)
- ブラウザ1タスク内の操作履歴

失効(expiry): term の終了日を過ぎた科目メモリは、cleanup で猶予期間後に削除。
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

DIR = Path.home() / ".study_agent"
MEM = DIR / "memory"
TERMS_FILE = MEM / "terms.json"
COURSES_FILE = MEM / "courses.json"
KEYCHAIN_SERVICE = "study_agent"


def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save(path: Path, data):
    MEM.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ------------------------------------------------------------------- terms
def add_term(name: str, start: str, end: str) -> None:
    """start/end は 'YYYY-MM-DD'。例: add_term('Fall 2026','2026-09-24','2026-12-13')"""
    datetime.strptime(start, "%Y-%m-%d")
    datetime.strptime(end, "%Y-%m-%d")
    terms = _load(TERMS_FILE, {"terms": []})
    terms["terms"] = [t for t in terms["terms"] if t["name"] != name]
    terms["terms"].append({"name": name, "start": start, "end": end})
    _save(TERMS_FILE, terms)


def list_terms() -> list[dict]:
    return _load(TERMS_FILE, {"terms": []})["terms"]


def current_term(on: date | None = None) -> dict | None:
    on = on or date.today()
    for t in list_terms():
        s = datetime.strptime(t["start"], "%Y-%m-%d").date()
        e = datetime.strptime(t["end"], "%Y-%m-%d").date()
        if s <= on <= e:
            return t
    return None


# ----------------------------------------------------------------- courses
def set_course(name: str, term: str, assignment_policy: str,
               sites: list[str] | None = None) -> None:
    """科目メモリを保存。assignment_policy = 初日に確認した課題の出され方。"""
    courses = _load(COURSES_FILE, {})
    courses[name] = {
        "term": term,
        "assignment_policy": assignment_policy,
        "sites": sites or [],
        "updated": date.today().isoformat(),
    }
    _save(COURSES_FILE, courses)


def get_course(name: str) -> dict | None:
    return _load(COURSES_FILE, {}).get(name)


def list_courses(term: str | None = None) -> dict:
    courses = _load(COURSES_FILE, {})
    if term is None:
        return courses
    return {k: v for k, v in courses.items() if v.get("term") == term}


def course_context(name: str) -> str:
    """プロンプト/ブラウザタスクに差し込む科目文脈テキスト。"""
    c = get_course(name)
    if not c:
        return ""
    return (f"[Course: {name} — {c['term']}]\n"
            f"Homework policy (from first day): {c['assignment_policy']}\n"
            f"Relevant sites: {', '.join(c['sites']) or 'n/a'}")


# ------------------------------------------------------------------- expiry
def cleanup_expired(grace_days: int = 30, delete_credentials: bool = False
                    ) -> list[str]:
    """終了したtermに属する科目メモリを猶予期間後に削除。削除した科目名を返す。"""
    terms = {t["name"]: t for t in list_terms()}
    today = date.today()
    courses = _load(COURSES_FILE, {})
    removed = []
    for name, c in list(courses.items()):
        term = terms.get(c.get("term"))
        if not term:
            continue
        end = datetime.strptime(term["end"], "%Y-%m-%d").date()
        if today > end + timedelta(days=grace_days):
            if delete_credentials:
                for site in c.get("sites", []):
                    delete_credential(site)
            del courses[name]
            removed.append(name)
    if removed:
        _save(COURSES_FILE, courses)
    return removed


# -------------------------------------------------------------- credentials
def _keyring():
    try:
        import keyring
        keyring.get_keyring()  # 利用可能かチェック
        return keyring
    except Exception:  # noqa: BLE001
        return None


def set_credential(site: str, username: str, password: str) -> str:
    """site のログイン情報を保存。戻り値は使用したバックエンド名。"""
    kr = _keyring()
    _index_add(site)
    if kr:
        kr.set_password(KEYCHAIN_SERVICE, f"{site}::user", username)
        kr.set_password(KEYCHAIN_SERVICE, f"{site}::pass", password)
        return "os-keychain"
    _fallback_set(site, username, password)
    return "encrypted-file (keychainなし)"


def get_credential(site: str) -> tuple[str, str] | None:
    kr = _keyring()
    if kr:
        u = kr.get_password(KEYCHAIN_SERVICE, f"{site}::user")
        p = kr.get_password(KEYCHAIN_SERVICE, f"{site}::pass")
        return (u, p) if p is not None else None
    return _fallback_get(site)


def delete_credential(site: str) -> None:
    kr = _keyring()
    if kr:
        for suffix in ("::user", "::pass"):
            try:
                kr.delete_password(KEYCHAIN_SERVICE, f"{site}{suffix}")
            except Exception:  # noqa: BLE001
                pass
        return
    _fallback_delete(site)


def list_credential_sites() -> list[str]:
    """保存済みサイト名の一覧(パスワード本体は返さない)。"""
    return _load(MEM / "cred_index.json", [])


def _index_add(site: str):
    idx = set(_load(MEM / "cred_index.json", []))
    idx.add(site)
    _save(MEM / "cred_index.json", sorted(idx))


# --- encrypted-file fallback (keychainが無い環境のみ。keychainより弱い) ---
_FKEY = DIR / ".fkey"
_FVAULT = MEM / "cred_vault.enc"


def _fernet():
    from cryptography.fernet import Fernet
    if not _FKEY.exists():
        DIR.mkdir(exist_ok=True)
        _FKEY.write_bytes(Fernet.generate_key())
        try:
            _FKEY.chmod(0o600)
        except OSError:
            pass
    return Fernet(_FKEY.read_bytes())


def _fallback_set(site, username, password):
    f = _fernet()
    vault = {}
    if _FVAULT.exists():
        vault = json.loads(f.decrypt(_FVAULT.read_bytes()).decode())
    vault[site] = {"u": username, "p": password}
    MEM.mkdir(parents=True, exist_ok=True)
    _FVAULT.write_bytes(f.encrypt(json.dumps(vault).encode()))
    try:
        _FVAULT.chmod(0o600)
    except OSError:
        pass
    _index_add(site)


def _fallback_get(site):
    if not _FVAULT.exists():
        return None
    f = _fernet()
    vault = json.loads(f.decrypt(_FVAULT.read_bytes()).decode())
    e = vault.get(site)
    return (e["u"], e["p"]) if e else None


def _fallback_delete(site):
    if not _FVAULT.exists():
        return
    f = _fernet()
    vault = json.loads(f.decrypt(_FVAULT.read_bytes()).decode())
    vault.pop(site, None)
    _FVAULT.write_bytes(f.encrypt(json.dumps(vault).encode()))
