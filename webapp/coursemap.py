"""科目の「文脈」を作る — シラバスから道具を読み取り、課題に担当を割り当てる。

やること2つ:

  1. extract_tools(text)
       シラバス本文から「この科目で使う道具」を拾う。
       例: "Programs must compile with g++ -std=c++17 and run clean under Valgrind"
           → C++17 / GCC / Valgrind

  2. allocate(kind)
       課題の種類に対して study_agent のどの部品が担当するかを返す。

**LLM は一切使わない。** 語彙表(TOOL_VOCAB)との単純な照合だけで作る。
理由は3つ:
  - 同期のたびに走るので、トークンを使うと積み上がって高くつく
  - 同じシラバスからは必ず同じ結果が出る(LLM だと毎回ぶれる)
  - シラバスが変わっていなければ作り直さない、という判定が素直に書ける

語彙を足したいときは TOOL_VOCAB に1行足すだけでよい。
"""

from __future__ import annotations

import json
import re

# ---------------------------------------------------------------- 語彙表
# (正規表現, 表示名, 分類)
#
# 表示名に "{ver}" を入れると、正規表現の名前付きグループ ver が差し込まれる。
# 例: C\+\+(?P<ver>17) → "C++17"
#
# 短すぎる名前(R, C, Go など)は普通の文章に紛れて誤検出するので、
# 必ず前後の語(RStudio / C programming / Go language)まで含めて書く。
TOOL_VOCAB: list[tuple[str, str, str]] = [
    # ---- 言語と規格
    (r"\bC\+\+\s*(?P<ver>0x|11|14|17|20|23)\b",        "C++{ver}",    "language"),
    (r"\bC\+\+(?!\w)",                                      "C++",         "language"),
    (r"\bC(?P<ver>89|99|11|17)\b(?!\s*\+)",            "C{ver}",      "language"),
    (r"\bC\s+programming\b",                            "C",           "language"),
    (r"\bPython\s*(?P<ver>3(?:\.\d+)?)\b",             "Python {ver}", "language"),
    (r"\bPython\b",                                     "Python",      "language"),
    (r"\bJavaScript\b|\bECMAScript\b",                  "JavaScript",  "language"),
    (r"\bTypeScript\b",                                 "TypeScript",  "language"),
    (r"\bJava\b(?!Script)",                             "Java",        "language"),
    (r"\bRust\b",                                       "Rust",        "language"),
    (r"\bGo(?:lang)?\s+(?:language|programming)\b|\bGolang\b", "Go",   "language"),
    (r"\bKotlin\b", "Kotlin", "language"),
    (r"\bSwift\b",  "Swift",  "language"),
    (r"\bMATLAB\b", "MATLAB", "language"),
    (r"\bRStudio\b|\bR\s+programming\b",                "R",           "language"),
    (r"\bSQL\b",    "SQL",    "language"),
    (r"\bAssembly\b|\bx86-64\b|\bMIPS\b",               "Assembly",    "language"),
    (r"\bHaskell\b", "Haskell", "language"),
    (r"\bScheme\b|\bRacket\b", "Racket/Scheme", "language"),

    # ---- コンパイラ・ビルド
    (r"\bg\+\+(?!\w)|\bGCC\b",  "GCC",    "build"),
    (r"\bclang\+\+(?!\w)|\bClang\b", "Clang", "build"),
    (r"\bCMake\b",  "CMake",  "build"),
    (r"\bMakefile\b|\bGNU\s+Make\b|\bmake\b(?=\s+(?:clean|all|test))", "Make", "build"),
    (r"\bGradle\b", "Gradle", "build"),
    (r"\bMaven\b",  "Maven",  "build"),
    (r"\bnpm\b|\byarn\b|\bpnpm\b", "npm", "build"),

    # ---- デバッグ・検査
    (r"\bValgrind\b", "Valgrind", "debug"),
    (r"\bGDB\b|\bgdb\b", "GDB", "debug"),
    (r"\bLLDB\b", "LLDB", "debug"),
    (r"\bAddressSanitizer\b|\bASan\b|-fsanitize", "AddressSanitizer", "debug"),

    # ---- テスト
    (r"\bJUnit\b", "JUnit", "testing"),
    (r"\bpytest\b|\bPyTest\b", "pytest", "testing"),
    (r"\bGoogle\s*Test\b|\bgtest\b", "GoogleTest", "testing"),
    (r"\bCatch2\b", "Catch2", "testing"),
    (r"\bunittest\b", "unittest", "testing"),

    # ---- ライブラリ・フレームワーク
    (r"\bNumPy\b",  "NumPy",  "library"),
    (r"\bpandas\b", "pandas", "library"),
    (r"\bscikit-learn\b|\bsklearn\b", "scikit-learn", "library"),
    (r"\bPyTorch\b", "PyTorch", "library"),
    (r"\bTensorFlow\b", "TensorFlow", "library"),
    (r"\bReact\b",  "React",  "library"),
    (r"\bFlask\b",  "Flask",  "library"),
    (r"\bDjango\b", "Django", "library"),
    (r"\bNode\.?js\b", "Node.js", "library"),
    (r"\bLucene\b|\bElasticsearch\b", "Lucene/Elasticsearch", "library"),
    (r"\bNLTK\b|\bspaCy\b", "NLTK/spaCy", "library"),

    # ---- 環境・基盤
    (r"\bDocker\b", "Docker", "platform"),
    (r"\bLinux\b|\bUbuntu\b|\bDebian\b", "Linux", "platform"),
    (r"\bWSL\b", "WSL", "platform"),
    (r"\bmacOS\b|\bOS\s*X\b", "macOS", "platform"),
    (r"\bPostgre(?:s|SQL)\b", "PostgreSQL", "platform"),
    (r"\bMySQL\b",  "MySQL",  "platform"),
    (r"\bMongoDB\b", "MongoDB", "platform"),
    (r"\bSQLite\b", "SQLite", "platform"),

    # ---- 提出先・道具
    (r"\bGradescope\b", "Gradescope", "submit"),
    (r"\bPrairieLearn\b", "PrairieLearn", "submit"),
    (r"\bzy[Bb]ooks\b", "zyBooks", "submit"),
    (r"\bGit[Hh]ub\b", "GitHub", "tooling"),
    (r"\bGitLab\b", "GitLab", "tooling"),
    (r"\bgit\b(?!hub|lab)", "Git", "tooling"),
    (r"\bLaTeX\b|\bOverleaf\b", "LaTeX", "tooling"),
    (r"\bJupyter\b|\bColab\b", "Jupyter", "tooling"),
    (r"\bAnaconda\b|\bconda\b", "Anaconda", "tooling"),
    (r"\bVS\s*Code\b|\bVisual\s+Studio\s+Code\b", "VS Code", "editor"),
    (r"\bIntelliJ\b", "IntelliJ", "editor"),
    (r"\bEclipse\b", "Eclipse", "editor"),
    (r"\bVim\b|\bEmacs\b", "Vim/Emacs", "editor"),

    # ---- 連絡手段(パスワードが要ることがあるので拾っておく)
    (r"\bPiazza\b", "Piazza", "comms"),
    (r"\bEd\s+Discussion\b|\bEdStem\b", "Ed Discussion", "comms"),
    (r"\bSlack\b", "Slack", "comms"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label, cat)
             for p, label, cat in TOOL_VOCAB]

# 分類の並び順。画面に出すときにこの順にそろえる。
CATEGORY_ORDER = ["language", "build", "debug", "testing", "library",
                  "platform", "tooling", "editor", "submit", "comms"]


def extract_tools(*texts: str) -> list[dict]:
    """シラバス(と課題の説明文)から、使う道具を拾って返す。

    返り値は [{"name": "C++17", "category": "language"}, ...]。
    同じ道具が何度出てきても1件にまとめる。並び順は CATEGORY_ORDER。

    より具体的な表記が勝つ: "C++17" が見つかったら、素の "C++" は落とす
    (両方出すと画面が読みにくいだけなので)。
    """
    blob = "\n".join(t for t in texts if t)
    if not blob.strip():
        return []

    found: dict[str, str] = {}       # 表示名 -> 分類
    for rx, label, cat in _COMPILED:
        m = rx.search(blob)
        if not m:
            continue
        name = label
        if "{ver}" in label:
            ver = (m.groupdict().get("ver") or "").strip()
            if not ver:
                continue
            name = label.replace("{ver}", ver)
        found.setdefault(name, cat)

    # versioned が居るなら素の名前は消す(C++17 があるとき C++ は要らない)
    for name in list(found):
        for other in found:
            if other != name and other.startswith(name) and other[len(name):][:1] in "+0123456789 ":
                found.pop(name, None)
                break

    def sort_key(item):
        name, cat = item
        pos = CATEGORY_ORDER.index(cat) if cat in CATEGORY_ORDER else len(CATEGORY_ORDER)
        return (pos, name.lower())

    return [{"name": n, "category": c} for n, c in sorted(found.items(), key=sort_key)]


def tools_to_json(tools: list[dict]) -> str:
    """DB の courses.tools 列に入れる形にする。"""
    return json.dumps(tools, ensure_ascii=False)


def tools_from_json(raw: str | None) -> list[dict]:
    """courses.tools を読み戻す。壊れていても落ちない(空扱い)。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("name")]


# ------------------------------------------------------------ 担当の割り当て
# 課題の種類 -> study_agent のどの部品が扱うか。
# app.py の _assignment_kind() が付ける種類と対応している。
#
# essay を空にしてあるのは指定どおり(長文は本人が書く)。ここを埋めない限り、
# エッセイがエージェントに回ることはない。
AGENT_ALLOCATION: dict[str, list[str]] = {
    "quiz":  ["quiz.py", "notes.py"],
    "short": ["assignment.py", "notes.py"],
    "essay": [],
    "other": ["assignment.py"],
}


def allocate(kind: str) -> list[str]:
    """その種類の課題を担当する study_agent の部品を返す。

    空リスト = エージェントは手を出さない(エッセイ)。
    """
    return list(AGENT_ALLOCATION.get(kind, AGENT_ALLOCATION["other"]))


def is_agent_handled(kind: str) -> bool:
    """エージェントが下書きを作れる種類かどうか。"""
    return bool(allocate(kind))
