"""UCI の Unofficial Transcript (HTML) を読んで、履修科目と GPA を取り出す。

■ このファイルの役割
学生が Student Access の「Unofficial Transcript」ページを ⌘S / Ctrl+S で
保存した .html を受け取り、

    学期 → 科目コード / 科目名 / 単位数 / 成績

の一覧に変換する。そこから GPA と「卒業単位(UCI は 180)までの進捗」を
計算して academic.html に渡す。AntAlmanac が読むのと同じファイルを想定。

■ なぜ「ゆるい」パーサなのか
Student Access の HTML は大学側の都合でいつ変わってもおかしくないので、
特定のクラス名や id には一切依存しない。やっていることは:

  1. HTML を「表の行」と「表の外のテキスト」に、出てきた順に並べ直す
  2. どこかに "Fall Quarter 2024" のような学期見出しが出たら、以降の行は
     その学期のものとみなす
  3. 各行について「科目コードらしいセル」「単位数らしい数値」
     「成績記号らしいセル」の 3 つが揃っていれば履修 1 件として拾う

これなら表の列順やタグ構成が変わっても壊れにくい。逆に、拾えたのが 0 件の
ときは「解析できませんでした」を返して、画面側で手入力に誘導する。

■ 外部依存
標準ライブラリの html.parser だけ。BeautifulSoup 等は入れていない。
"""
import re
from html.parser import HTMLParser

from school import counts_as_earned, get_school, grade_to_points

# "Fall Quarter 2024" / "Winter 2025" / "Summer Session 1 2024" などを拾う
TERM_RE = re.compile(
    r"\b(Fall|Winter|Spring|Summer)\b[\s\w]{0,20}?\b(19|20)(\d{2})\b",
    re.IGNORECASE)

# "I&C SCI 31" "MATH 2A" "WRITING 40" "UNI STU 3" "ICS 45C" のような科目コード
COURSE_CODE_RE = re.compile(r"^[A-Z][A-Z&/.]*(?:\s+[A-Z&/.]+)*\s+\d{1,3}[A-Z]{0,3}$")

# 成績記号。GPA に入らないもの(P, NP, W ...)も「行を見つける手がかり」として拾う
GRADE_RE = re.compile(
    r"^(A\+|A-|A|B\+|B-|B|C\+|C-|C|D\+|D-|D|F|P|NP|S|U|CR|NC|I|IP|W|NR)$")

UNITS_RE = re.compile(r"^\d{1,2}(\.\d{1,2})?$")

# 学期の並び順(表示とソートに使う)
_SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


class _Blocks(HTMLParser):
    """HTML を「表の行(セルの配列)」と「表の外のテキスト」に、出現順で分解する。

    self.blocks の中身は次のどちらか:
        {"type": "row",  "cells": ["I&C SCI 31", "INTRO TO PROG", "4.0", "A"]}
        {"type": "text", "text":  "Fall Quarter 2024"}
    """

    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._cells: list[str] | None = None   # <tr> の中にいるとき、集めたセル
        self._cell: list[str] | None = None    # <td>/<th> の中にいるとき、その文字
        self._loose: list[str] = []            # 表の外のテキスト
        self._skip_depth = 0

    # -- タグ ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "tr":
            self._flush_loose()
            self._cells = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in ("br", "p", "div", "tr"):
            self._loose.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("td", "th"):
            if self._cell is not None and self._cells is not None:
                self._cells.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr":
            if self._cells:
                self.blocks.append({"type": "row", "cells": self._cells})
            self._cells = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._cells is None:
            self._loose.append(data)

    # -- 後始末 -------------------------------------------------------
    def _flush_loose(self):
        text = _clean(" ".join(self._loose))
        self._loose = []
        if text:
            self.blocks.append({"type": "text", "text": text})

    def close(self):
        super().close()
        self._flush_loose()


def _clean(s: str) -> str:
    """空白・改行・NBSP を 1 個のスペースに潰す。"""
    return re.sub(r"[\s ]+", " ", s).strip()


def _normalize_term(raw: str) -> str | None:
    """テキストから学期名を取り出して "Fall 2024" の形に揃える。無ければ None。"""
    m = TERM_RE.search(raw)
    if not m:
        return None
    season = m.group(1).capitalize()
    year = f"{m.group(2)}{m.group(3)}"
    return f"{season} {year}"


def term_sort_key(term: str) -> tuple:
    """"Fall 2024" → (2024, 3)。学期タブを時系列に並べるのに使う。"""
    parts = term.split()
    if len(parts) != 2:
        return (0, 0)
    season, year = parts
    try:
        return (int(year), _SEASON_ORDER.get(season.lower(), 9))
    except ValueError:
        return (0, 0)


def _parse_row(cells: list[str]) -> dict | None:
    """表の 1 行が履修 1 件なら dict にして返す。違えば None。

    「科目コード → (科目名) → 単位数 → 成績」の順に並んでいることだけを
    前提にする(間に他の列が挟まっていてもよい)。
    """
    if len(cells) < 3:
        return None

    code_idx = next(
        (i for i, c in enumerate(cells) if COURSE_CODE_RE.match(c.upper())), None)
    if code_idx is None:
        return None

    # 成績はコードより後ろにあるものだけを見る(最後に出てきたものを採用)
    grade_idx = None
    for i in range(len(cells) - 1, code_idx, -1):
        if GRADE_RE.match(cells[i].upper()):
            grade_idx = i
            break
    if grade_idx is None:
        return None

    # 単位数はコードと成績の間にある数値。複数あれば最後(= 成績に近い方)。
    units = None
    units_idx = None
    for i in range(code_idx + 1, grade_idx):
        if UNITS_RE.match(cells[i]):
            value = float(cells[i])
            if 0 < value <= 25:
                units, units_idx = value, i
    if units is None:
        return None

    # 科目名はコードと単位数の間にある、数値でないいちばん長いセル
    title_cells = [cells[i] for i in range(code_idx + 1, units_idx)
                   if cells[i] and not UNITS_RE.match(cells[i])]
    title = max(title_cells, key=len) if title_cells else ""

    return {
        "code": _clean(cells[code_idx].upper()),
        "title": title,
        "units": units,
        "grade": cells[grade_idx].upper(),
    }


def parse_transcript_html(html: str, school_name: str | None = None) -> dict:
    """成績表 HTML → {"courses": [...], "terms": [...], "error": str|None}

    courses の各要素:
        {"term", "code", "title", "units", "grade", "points"}
        points は GPA ポイント(P/NP など GPA 外の記号なら None)
    """
    school = get_school(school_name)

    parser = _Blocks()
    parser.feed(html)
    parser.close()

    courses: list[dict] = []
    current_term = "Unknown"

    for block in parser.blocks:
        if block["type"] == "text":
            term = _normalize_term(block["text"])
            if term:
                current_term = term
            continue

        cells = block["cells"]
        # 見出し行が <td> で書かれていることもあるので、行のテキストも学期として見る
        joined = " ".join(cells)
        row = _parse_row(cells)
        if row is None:
            term = _normalize_term(joined)
            if term:
                current_term = term
            continue

        # 履修行そのものに学期が書かれている形式にも一応対応する
        row_term = _normalize_term(joined)
        row["term"] = row_term or current_term
        row["points"] = grade_to_points(row["grade"], school)
        courses.append(row)

    if not courses:
        return {"courses": [], "terms": [], "error":
                "この HTML から履修科目を読み取れませんでした。"
                "Student Access の「Unofficial Transcript」ページを保存した "
                ".html か確認してください。下の手入力でも登録できます。"}

    terms = sorted({c["term"] for c in courses}, key=term_sort_key)
    return {"courses": courses, "terms": terms, "error": None}


def compute_gpa(courses: list[dict], school_name: str | None = None) -> dict:
    """履修一覧 → GPA と単位数のまとめ。

    返す dict:
        gpa            … 累積 GPA(GPA 対象科目のみ)
        gpa_units      … GPA 計算に入った単位数
        grade_points   … 総グレードポイント(= Σ 単位 × ポイント)
        units_earned   … 取得できた単位数(P/S などの合格も含む)
        units_attempted… 履修した単位数の合計
        remaining      … 卒業まで残り単位数(0 未満にはならない)
        progress_pct   … 卒業単位に対する達成率(0-100)
    """
    school = get_school(school_name)
    goal = school["graduation_units"]

    grade_points = 0.0
    gpa_units = 0.0
    units_earned = 0.0
    units_attempted = 0.0

    for c in courses:
        units = float(c.get("units") or 0)
        units_attempted += units
        grade = c.get("grade", "")
        # ポイントは必ず成績記号から引き直す。DB から読んだ行には "points" が
        # 入っていない(保存しているのは成績記号だけ)ため、c["points"] に
        # 頼ると GPA が 0 になる。
        points = grade_to_points(grade, school)
        if points is not None:
            grade_points += units * points
            gpa_units += units
        if counts_as_earned(grade, school):
            units_earned += units

    gpa = round(grade_points / gpa_units, 3) if gpa_units else 0.0
    return {
        "gpa": gpa,
        "gpa_units": round(gpa_units, 2),
        "grade_points": round(grade_points, 3),
        "units_earned": round(units_earned, 2),
        "units_attempted": round(units_attempted, 2),
        "goal_units": goal,
        "remaining": round(max(0.0, goal - units_earned), 2),
        "progress_pct": round(min(100.0, units_earned / goal * 100), 1) if goal else 0.0,
    }


def group_by_term(courses: list[dict], school_name: str | None = None) -> list[dict]:
    """学期ごとにまとめ、各学期の GPA も付けて新しい順に並べる。

    academic.html の「学期ごとの表」をそのまま描ける形にして返す。
    """
    buckets: dict[str, list[dict]] = {}
    for c in courses:
        buckets.setdefault(c.get("term", "Unknown"), []).append(c)

    out = []
    for term, rows in buckets.items():
        summary = compute_gpa(rows, school_name)
        out.append({
            "term": term,
            "courses": sorted(rows, key=lambda r: r["code"]),
            "gpa": summary["gpa"],
            "units": summary["units_attempted"],
        })
    out.sort(key=lambda t: term_sort_key(t["term"]), reverse=True)
    return out
