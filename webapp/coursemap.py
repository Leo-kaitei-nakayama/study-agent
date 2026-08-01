"""科目の「文脈」を作る — 使う道具と、課題ごとの担当。

**語彙表と抽出そのものは study_agent/planner.py にある。** ここはそれを
webapp 側の名前で使えるようにしているだけ(二重管理を避けるため)。
このファイルが自前で持っているのは webapp 固有の 2 つ:

  1. NON_COURSE_NAMES / is_real_course()
       Canvas の画面名(Dashboard など)を科目として取り込まない
  2. AGENT_ALLOCATION / allocate()
       課題の種類に対して study_agent のどの部品が担当するか
"""

from __future__ import annotations

import sys
from pathlib import Path

# webapp/ から study_agent/ を見えるようにする(app.py と同じ扱い)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from study_agent.planner import (  # noqa: E402
    CATEGORY_ORDER,
    TOOL_VOCAB,
    build_tool_strategy,
    extract_tools,
    strategy_from_json,
    tools_from_json,
    tools_to_json,
)

__all__ = [
    "CATEGORY_ORDER", "TOOL_VOCAB", "build_tool_strategy", "extract_tools",
    "strategy_from_json", "tools_from_json", "tools_to_json",
    "NON_COURSE_NAMES", "is_real_course", "AGENT_ALLOCATION", "allocate",
    "is_agent_handled",
]


# ------------------------------------------------------ 科目ではない画面の名前
# 拡張機能の content.js は、科目名が分からないときページのタイトルから推測する。
# そのためダッシュボードや受信トレイを開いていると、「Dashboard」という名前の
# 科目ができてしまう。これらは授業ではないので取り込まない。
#
# 学生が自分で選んだ科目名(画面のプルダウン)には適用しない。ここで弾くのは
# 拡張機能が勝手に推測してきた名前だけ。
NON_COURSE_NAMES = {
    "dashboard", "calendar", "inbox", "courses", "groups", "account",
    "history", "help", "commons", "home", "notifications", "profile",
    "settings", "canvas", "my dashboard", "course dashboard",
    "all courses", "recent activity",
}


def is_real_course(name: str | None) -> bool:
    """その名前を科目として取り込んでよいか。

    Canvas の画面名(Dashboard など)や、空・短すぎるものは科目ではない。
    """
    if not name:
        return False
    cleaned = " ".join(name.split()).strip(" -–—·:|")
    if len(cleaned) < 2:
        return False
    return cleaned.casefold() not in NON_COURSE_NAMES


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
