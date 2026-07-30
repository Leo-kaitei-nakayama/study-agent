"""練習課題のドラフト作成モジュール。

方針: このエージェントは課題を「自動提出」しない。
出力は必ず DRAFT (下書き) として .docx に保存し、本人がレビュー・修正してから使う。
解答だけでなく解説も付けるので、自習用としてそのまま使える。
"""
from datetime import date
from pathlib import Path

from . import llm
from .docx_writer import markdown_to_docx

_SYSTEM = """あなたは大学の課題を手伝うチューターです。
与えられた練習課題に対して、模範解答のドラフトと学習用の解説を作成してください。

ルール:
- 出力言語: {lang}
- 各問について「## 問題n」→ 解答 → 「### 解説」(なぜその解答になるか、使う概念) の順
- コーディング課題ならコードと動作説明、記述式なら構成された文章で
- 不確かな点・問題文が曖昧な点は明示する
- 最後に「## レビュー時のチェックポイント」として、提出前に本人が確認・修正すべき箇所を挙げる
- Markdown本文のみを出力"""

_BANNER = ("⚠ DRAFT — 自動生成された下書きです。内容を自分で確認・理解・修正してから使うこと。"
           "成績に関わる提出物にそのまま使わないこと。")


def make_draft(input_path: str, out_path: str | None = None,
               lang: str = "日本語", extra_instructions: str = "",
               provider: str = "auto", api_keys: dict | None = None,
               usage_callback=None, routing: dict | None = None) -> str:
    from .extract import extract_text

    text = extract_text(input_path)
    user = text
    if extra_instructions:
        user += f"\n\n[追加指示]\n{extra_instructions}"

    print("  ドラフト生成中...")
    md = llm.complete(_SYSTEM.format(lang=lang), user, max_tokens=8000,
                      provider=provider, api_keys=api_keys,
                      usage_callback=usage_callback, routing=routing)

    src = Path(input_path)
    out = out_path or str(src.with_name(src.stem + "_draft.docx"))
    return markdown_to_docx(
        md, out,
        title=f"{src.stem} — Draft ({date.today().isoformat()})",
        banner=_BANNER,
    )
