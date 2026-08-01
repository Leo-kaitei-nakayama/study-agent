"""生成物(.docx / .md / .txt)を、ダウンロードせずブラウザで読める形にする。

■ このファイルの役割
ノート一覧の「表示」ボタンから、中身をその場で確認できるようにする。
ダウンロードしてから Word で開かないと内容が分からない、という状態を無くす。

出力は **HTML の断片**(<h2> や <p> の並び)で、note_view.html に埋め込む。
安全のため、元テキストは必ずエスケープしてからタグを組み立てる
(生成物には LLM の出力が入るので、HTML をそのまま通してはいけない)。

■ 対応する形式
  .docx        … python-docx で段落を取り出す(study_agent.extract を利用)
  .md / .txt   … 見出し・箇条書き・太字・コードだけの軽い Markdown 変換

フル機能の Markdown ライブラリを入れていないのは、生成物に出てくる記法が
限られていて、依存を増やす価値が薄いため。表など未対応の記法は、その行が
そのまま出るだけで壊れはしない。
"""
import html
import re
from pathlib import Path

# 1 回のプレビューで読み込む上限。巨大なファイルで画面が固まらないようにする。
MAX_CHARS = 200_000


def load_text(path: str | Path) -> tuple[str, bool]:
    """ファイル → 素のテキスト。(text, 切り詰めたか) を返す。"""
    p = Path(path)
    suffix = p.suffix.lower()

    if suffix in (".md", ".txt", ""):
        text = p.read_text(encoding="utf-8", errors="replace")
    else:
        # .docx / .pdf / .pptx は study_agent 側の抽出を使い回す
        from study_agent.extract import extract_text
        text = extract_text(str(p))

    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, False


def to_html(text: str) -> str:
    """素のテキスト → 表示用の HTML 断片。

    入力は必ずエスケープしてから組み立てるので、生成物に HTML が混ざっていても
    タグとして解釈されることはない。
    """
    out: list[str] = []
    in_list = False
    in_code = False

    # 表は次の行(区切り行)まで見ないと判断できないので、添字で回す。
    lines = text.splitlines()
    i = -1
    while True:
        i += 1
        if i >= len(lines):
            break
        line = lines[i].rstrip()

        # ``` で囲まれたコードブロック
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append(_close_list(out, in_list) or "")
                in_list = False
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue

        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue

        # 見出し (#, ##, ###)
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            level = min(len(m.group(1)) + 1, 6)   # # は h2 から(h1 はページ名)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue

        # 箇条書き (-, *, +, 1.)
        m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # 表 (| a | b | / |---|---| / | 1 | 2 |)
        # 課題一覧を1行1件で読めるようにするために足した。区切り行
        # (|---|---|) が続いていることを見て、表の始まりだと判断する。
        if _is_table_row(line) and _is_table_divider(_peek(lines, i + 1)):
            if in_list:
                out.append("</ul>")
                in_list = False
            head = _split_row(line)
            out.append("<table><thead><tr>")
            out.extend(f"<th>{_inline(c)}</th>" for c in head)
            out.append("</tr></thead><tbody>")
            i += 2                                  # 見出しと区切り行を飛ばす
            while i < len(lines) and _is_table_row(lines[i].rstrip()):
                out.append("<tr>")
                out.extend(f"<td>{_inline(c)}</td>" for c in _split_row(lines[i].rstrip()))
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        # 区切り線
        if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr>")
            continue

        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{_inline(line)}</p>")

    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(x for x in out if x)


def _close_list(out: list, in_list: bool) -> str | None:
    return "</ul>" if in_list else None


# ---------------------------------------------------------------- 表の判定
def _peek(lines: list[str], idx: int) -> str:
    return lines[idx].rstrip() if 0 <= idx < len(lines) else ""


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_table_divider(line: str) -> bool:
    """|---|:--:|---| のような区切り行か。"""
    s = line.strip()
    if not _is_table_row(s):
        return False
    return all(re.fullmatch(r":?-{2,}:?", c.strip() or "-")
               for c in _split_row(s))


def _split_row(line: str) -> list[str]:
    """| a | b | → ["a", "b"]。前後の | は落とす。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _inline(text: str) -> str:
    """行の中の **太字** / *斜体* / `コード` だけを変換する。

    先にエスケープするので、元テキストの <b> などはタグにならない。
    """
    s = html.escape(text)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    return s


def render(path: str | Path) -> dict:
    """プレビュー用のまとめ。テンプレートにそのまま渡せる形。

    返す dict:
        html      … 表示用の HTML 断片
        truncated … 長すぎて切り詰めたか
        error     … 読めなかったときの理由(読めたら None)
    """
    try:
        text, truncated = load_text(path)
    except Exception as e:  # noqa: BLE001 — 壊れたファイルでも画面は出す
        return {"html": "", "truncated": False, "error": str(e)}
    return {"html": to_html(text), "truncated": truncated, "error": None}
