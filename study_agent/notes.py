"""教科書・授業資料 → 構造化ノート (.docx)。"""
from pathlib import Path

from . import llm
from .docx_writer import markdown_to_docx
from .extract import chunk_text, extract_text

_SYSTEM = """あなたは優秀な大学生向けのノートテイカーです。
与えられた教材テキストから、復習に使える構造化ノートをMarkdownで作成してください。

ルール:
- 出力言語: {lang}
- 「## セクション見出し」で章立てし、箇条書き中心でまとめる
- 重要用語は **太字** にし、短い定義を添える
- 数式・手順・例題があれば省略せず含める
- 教材にない内容を創作しない。曖昧な箇所は (原文不明瞭) と記す
- 最後に「## 重要ポイントまとめ」と「## 予想される試験ポイント」を付ける
- Markdown本文のみを出力(前置き・後書き不要)"""

_MERGE_SYSTEM = """複数チャンクから作成されたノート断片を、重複を除き一貫した1つのノートに統合してください。
構成・ルールは元のノートと同じ。Markdown本文のみを出力。出力言語: {lang}"""


def make_notes(input_path: str, out_path: str | None = None,
               lang: str = "日本語", style: str = "detailed",
               provider: str = "auto", api_keys: dict | None = None,
               usage_callback=None, routing: dict | None = None) -> str:
    text = extract_text(input_path)
    chunks = chunk_text(text)
    system = _SYSTEM.format(lang=lang)
    if style == "summary":
        system += "\n- 詳細よりも要点の簡潔さを優先する(全体で1〜2ページ相当)"

    parts = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  ノート生成中... ({i}/{len(chunks)})")
        parts.append(llm.complete(system, chunk, max_tokens=6000,
                                  provider=provider, api_keys=api_keys,
                                  usage_callback=usage_callback, routing=routing))

    if len(parts) == 1:
        md = parts[0]
    else:
        print("  チャンクを統合中...")
        md = llm.complete(
            _MERGE_SYSTEM.format(lang=lang),
            "\n\n---\n\n".join(parts),
            max_tokens=8000, provider=provider, api_keys=api_keys,
            usage_callback=usage_callback, routing=routing,
        )

    src = Path(input_path)
    out = out_path or str(src.with_name(src.stem + "_notes.docx"))
    return markdown_to_docx(md, out, title=f"{src.stem} — Notes")
