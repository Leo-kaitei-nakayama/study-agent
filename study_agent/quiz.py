"""クイズ/小テスト回答支援。

方針(ユーザー指定): まずその日に作ったノートを文脈にして答える。
ノートで足りない/AIだけでは解けない場合に限り、通常のAI推論にフォールバック。
"""
from datetime import date
from pathlib import Path

from . import llm
from .extract import extract_text

_SYSTEM = """あなたは学習中の学生を助けるチューターです。
下記の「参考ノート」を最優先の根拠として、クイズ問題に答えてください。

ルール:
- 出力言語: {lang}
- 各問「## 問題n」→ 解答 → 根拠(ノートのどの部分に基づくか)
- ノートに答えの根拠がある問題には (ノート由来) と付ける
- ノートに無く一般知識で補った場合は (ノート外・要確認) と明示する
- 確信が持てない問題はそう書く。でっち上げない
- Markdown本文のみ"""


def _todays_notes(notes_dir: str, course: str | None) -> str:
    """今日作成したノート(.docx/.md/.txt)を集めて連結。"""
    d = Path(notes_dir)
    if not d.exists():
        return ""
    today = date.today().isoformat()
    texts = []
    for f in d.iterdir():
        if not f.is_file():
            continue
        made_today = date.fromtimestamp(f.stat().st_mtime).isoformat() == today
        matches_course = (course is None) or (course.lower().replace(" ", "")
                                              in f.name.lower().replace(" ", ""))
        if made_today and matches_course and f.suffix.lower() in (
                ".docx", ".md", ".txt"):
            try:
                texts.append(f"[{f.name}]\n{extract_text(str(f))}")
            except Exception:  # noqa: BLE001
                pass
    return "\n\n".join(texts)


def answer_quiz(quiz_path: str, notes_dir: str = ".",
                course: str | None = None, lang: str = "日本語",
                provider: str = "auto", api_keys: dict | None = None,
                usage_callback=None, routing: dict | None = None) -> str:
    quiz = extract_text(quiz_path)
    notes = _todays_notes(notes_dir, course)
    context = notes if notes else "(該当するノートが見つかりませんでした)"
    user = f"参考ノート:\n{context}\n\n---\nクイズ問題:\n{quiz}"
    return llm.complete(_SYSTEM.format(lang=lang), user,
                        max_tokens=6000, provider=provider, api_keys=api_keys,
                        usage_callback=usage_callback, routing=routing)
