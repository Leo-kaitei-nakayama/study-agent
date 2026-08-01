"""UCI の Unofficial Transcript (HTML) を読んで、履修科目と GPA を取り出す。

■ このファイルの役割
学生が Student Access の「Unofficial Transcript」ページを ⌘S / Ctrl+S で
保存した .html を受け取り、

    学期 → 科目コード / 科目名 / 単位数 / 成績

の一覧に変換する。そこから GPA と「卒業単位(UCI は 180)までの進捗」を
計算して academic.html に渡す。AntAlmanac が読むのと同じファイルを想定。

■ 実際の UCI の HTML について(重要)
Student Access の成績表は、**同じ履修を 3 つの表示形式で重複して持っている**:

    <div id="chrono-view">  … 学期順(既定で表示されている)
    <div id="school-view">  … 学部・学科ごと(display:none)
    <div id="modali-view">  … 対面 / オンラインごと(display:none)

3 つとも同じ科目が並ぶので、全部拾うと単位が 3 倍になる。そこで
**chrono-view の中だけ**を解析する。この id が無い HTML(他大学や、
将来 UCI が変えた場合)は文書全体を対象にフォールバックする。

■ 行の形
UCI の履修行はこうなっている(科目名が先、学科と番号が別セル):

    ['', 'BOOL LOG & DISC STR', 'I&C SCI', '6B', '4.0', 'A', '16.0', 'PN', ...]
      └ 科目名        └ 学科    └ 番号  └単位 └成績 └ポイント └P/NP等

学期見出しは "2024 Fall Quarter" のように **年が先**。合成テストデータで
使っている "Fall Quarter 2024" 形式も両方受け付ける。

■ なぜ「ゆるい」パーサなのか
列の順序やタグ構成が変わっても壊れないよう、クラス名には依存せず
「単位数らしい数値のすぐ後ろに成績記号がある」ことを手がかりに行を見つけ、
そこから前方に向かって科目コードと科目名を探す。拾えたのが 0 件のときは
「解析できませんでした」を返して、画面側で手入力に誘導する。

■ 公式の集計値
文書末尾に大学が計算した集計がある:

    GRADE UNITS ATTEMPTED 98.0 / GRADE POINTS 376.3 / UC GPA 3.840
    TOTAL UNITS PASSED 98.0 / UNITS COMPLETED 112.3

編入単位(他大学からの移行分)は個別の履修行としては載っていないため、
自前で足し上げた GPA は公式値とわずかにずれる。**公式値があればそちらを
正**として表示し、無いときだけ履修行から計算する。

■ 外部依存
標準ライブラリの html.parser だけ。BeautifulSoup 等は入れていない。
"""
import re
from html.parser import HTMLParser

from school import counts_as_earned, get_school, grade_to_points

# 学期見出しに使う季節と年。順序はどちらでもよい(_normalize_term 参照)。
SEASON_RE = re.compile(r"\b(Fall|Winter|Spring|Summer)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# "I&C SCI 31" "MATH 2A" "IN4MATX 43" のように 1 セルに収まった科目コード。
# 学科名の途中に数字が入るもの(IN4MATX)があるので数字も許す。ただし
# 先頭は必ず英字なので、"2024 Fall Quarter" のような見出しには一致しない。
COURSE_CODE_RE = re.compile(
    r"^[A-Z][A-Z0-9&/.]*(?:\s+[A-Z0-9&/.]+)*\s+\d{1,3}[A-Z]{0,3}$")

# 学科だけのセル("I&C SCI" "MATH" "UNI AFF" "AC ENG" "POL SCI" "IN4MATX")
DEPT_RE = re.compile(r"^[A-Z][A-Z0-9&/.]*(?:\s+[A-Z0-9&/.]+)*$")

# 科目番号だけのセル("6B" "7" "2A" "45C" "105" "30A")
COURSE_NUM_RE = re.compile(r"^\d{1,3}[A-Z]{0,3}$")

# 成績記号。GPA に入らないもの(P, NP, W ...)も「行を見つける手がかり」として拾う
GRADE_RE = re.compile(
    r"^(A\+|A-|A|B\+|B-|B|C\+|C-|C|D\+|D-|D|F|P|NP|S|U|CR|NC|I|IP|W|NR)$")

UNITS_RE = re.compile(r"^\d{1,2}(\.\d{1,2})?$")

# 集計行。履修行と間違えないように、最初のセルがこれなら丸ごと飛ばす。
TOTALS_RE = re.compile(
    r"^(Term|Cumulative|Dept|School|Transfer)\s+Totals$", re.IGNORECASE)

# 履修中でまだ成績が出ていない科目に入れる印。
# GPA にも取得単位にも数えず、「履修中」として別に集計する。
IN_PROGRESS = "N/A"
IN_PROGRESS_GRADES = {"N/A", "NA", "IP", "IN PROGRESS", ""}

# 小数点つきの単位数("4.0")。整数だけの列(セクション番号など)と区別するのに使う。
DECIMAL_UNITS_RE = re.compile(r"^\d{1,2}\.\d{1,2}$")

# 大学が計算した公式の集計値。ラベル → 内部で使う名前。
OFFICIAL_LABELS = {
    "UC GPA": "gpa",
    "GRADE UNITS ATTEMPTED": "gpa_units",
    "GRADE POINTS": "grade_points",
    "UNITS COMPLETED": "units_completed",
    "TOTAL UNITS PASSED": "units_passed",
}

# 学期の並び順(表示とソートに使う)
_SEASON_ORDER = {"winter": 0, "spring": 1, "summer": 2, "fall": 3}


class _Blocks(HTMLParser):
    """HTML を「表の行(セルの配列)」と「表の外のテキスト」に、出現順で分解する。

    self.blocks の中身は次のどちらか:
        {"type": "row",  "cells": ["I&C SCI 31", "INTRO TO PROG", "4.0", "A"]}
        {"type": "text", "text":  "2024 Fall Quarter"}

    only_within_id を渡すと、その id を持つ要素の中にある行だけを拾う。
    UCI の成績表は同じ履修を 3 つの div に重複して持っているので、
    chrono-view だけを見るために使う。
    """

    SKIP = {"script", "style", "head"}

    def __init__(self, only_within_id: str | None = None):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.found_section = False           # 目的の id が見つかったか
        self._only_id = only_within_id
        self._capturing = only_within_id is None
        self._section_depth = 0              # 目的の要素の入れ子の深さ
        self._cells: list[str] | None = None  # <tr> の中にいるとき、集めたセル
        self._cell: list[str] | None = None   # <td>/<th> の中にいるとき、その文字
        self._loose: list[str] = []           # 表の外のテキスト
        self._skip_depth = 0

    # -- タグ ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self._only_id is not None and tag == "div":
            if not self._capturing:
                if dict(attrs).get("id") == self._only_id:
                    self._capturing = True
                    self.found_section = True
                    self._section_depth = 1
                    return
            else:
                self._section_depth += 1

        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "tr":
            self._flush_loose()
            self._cells = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in ("br", "p", "div"):
            self._loose.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("td", "th"):
            if self._cell is not None and self._cells is not None:
                self._cells.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr":
            if self._cells and self._capturing:
                self.blocks.append({"type": "row", "cells": self._cells})
            self._cells = None

        # 目的の div を抜けたら、そこで採取をやめる
        if self._only_id is not None and tag == "div" and self._capturing:
            self._section_depth -= 1
            if self._section_depth <= 0:
                self._capturing = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._cells is None and self._capturing:
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
    """テキストから学期名を取り出して "Fall 2024" の形に揃える。無ければ None。

    年と季節が両方あればよく、順序は問わない:
        "2024 Fall Quarter"                    → Fall 2024   (UCI の実物)
        "Fall Quarter 2024"                    → Fall 2024
        "2024 Special / 10-Week Summer Session" → Summer 2024
    見出しではない長い文章を誤って学期と見なさないよう、長さで足切りする。
    """
    if not raw or len(raw) > 60:
        return None
    season = SEASON_RE.search(raw)
    year = YEAR_RE.search(raw)
    if not (season and year):
        return None
    return f"{season.group(1).capitalize()} {year.group(0)}"


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

    手がかりは「単位数らしい数値のすぐ後ろに成績記号がある」こと。そこを
    見つけてから、前方に向かって科目コードと科目名を探す。科目コードは
    2 通りの持ち方に対応する:

        1 セル  : ['I&C SCI 31', 'INTRO TO PROG', '4.0', 'A']
        2 セル  : ['', 'BOOL LOG & DISC STR', 'I&C SCI', '6B', '4.0', 'A', ...]
    """
    if len(cells) < 3 or TOTALS_RE.match(cells[0].strip()):
        return None

    # --- 単位数 + 成績 の並びを探す(最初に見つかったものを採る) -----------
    units = units_idx = grade_idx = None
    for i in range(len(cells) - 1):
        if not UNITS_RE.match(cells[i]) or not GRADE_RE.match(cells[i + 1].upper()):
            continue
        value = float(cells[i])
        if 0 < value <= 25:
            units, units_idx, grade_idx = value, i, i + 1
            break
    if units is None:
        return None

    # --- 科目コード ------------------------------------------------------
    code = None
    code_start = code_end = None

    # (a) 単位数の直前が「学科 + 番号」の 2 セルに分かれている形(UCI の実物)
    if units_idx >= 2:
        dept, num = cells[units_idx - 2].strip(), cells[units_idx - 1].strip()
        if DEPT_RE.match(dept.upper()) and COURSE_NUM_RE.match(num.upper()):
            code = f"{dept.upper()} {num.upper()}"
            code_start, code_end = units_idx - 2, units_idx - 1

    # (b) 1 セルに収まっている形
    if code is None:
        for i in range(units_idx - 1, -1, -1):
            if COURSE_CODE_RE.match(cells[i].strip().upper()):
                code = _clean(cells[i].upper())
                code_start = code_end = i
                break
    if code is None:
        return None

    # --- 科目名(コードの前後に残っている、数値でないいちばん長いセル) ------
    candidates = [cells[i] for i in range(0, code_start)] + \
                 [cells[i] for i in range(code_end + 1, units_idx)]
    candidates = [c.strip() for c in candidates
                  if c.strip() and not UNITS_RE.match(c.strip())]
    title = max(candidates, key=len) if candidates else ""

    return {
        "code": code,
        "title": title,
        "units": units,
        "grade": cells[grade_idx].upper(),
    }


def _parse_official_totals(html: str) -> dict:
    """大学が計算済みの集計値(UC GPA など)を文書全体から拾う。

    "ラベル" と "値" が隣り合うセルに入っているので、その並びを探す:
        ['GRADE UNITS ATTEMPTED', '98.0', 'GRADE POINTS', '376.3', 'UC GPA', '3.840']
    見つからなければ空の dict(呼び出し側は履修行から自前で計算する)。
    """
    parser = _Blocks()
    parser.feed(html)
    parser.close()

    found: dict[str, float] = {}
    for block in parser.blocks:
        if block["type"] != "row":
            continue
        cells = block["cells"]
        for i, cell in enumerate(cells[:-1]):
            key = OFFICIAL_LABELS.get(cell.strip().upper().rstrip(":"))
            if not key or key in found:
                continue
            value = cells[i + 1].strip()
            if re.match(r"^\d{1,4}(\.\d{1,3})?$", value):
                found[key] = float(value)
    return found


def parse_transcript_html(html: str, school_name: str | None = None) -> dict:
    """成績表 HTML → {"courses": [...], "terms": [...], "official": {...}, "error": ...}

    courses の各要素:
        {"term", "code", "title", "units", "grade", "points"}
        points は GPA ポイント(P/NP など GPA 外の記号なら None)

    official は大学が計算済みの集計値(あれば)。詳細はモジュール冒頭のコメント。
    """
    school = get_school(school_name)

    # UCI は同じ履修を 3 つの div に重複して持つので、学期順の view だけを見る。
    # その id が無い HTML では文書全体を対象にする。
    parser = _Blocks(only_within_id="chrono-view")
    parser.feed(html)
    parser.close()
    if not parser.found_section:
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
        row = _parse_row(cells)
        if row is None:
            # 見出し行が <td> で書かれていることもあるので、行のテキストも学期として見る
            term = _normalize_term(" ".join(c for c in cells if c.strip()))
            if term:
                current_term = term
            continue

        row["term"] = current_term
        row["points"] = grade_to_points(row["grade"], school)
        courses.append(row)

    if not courses:
        return {"courses": [], "terms": [], "official": {}, "error":
                "この HTML から履修科目を読み取れませんでした。"
                "Student Access の「Unofficial Transcript」ページを保存した "
                ".html か確認してください。下の手入力でも登録できます。"}

    terms = sorted({c["term"] for c in courses}, key=term_sort_key)
    return {"courses": courses, "terms": terms,
            "official": _parse_official_totals(html), "error": None}


def _find_course_code(cells: list[str], before: int) -> tuple[str, int, int] | None:
    """cells[:before] から科目コードを探す。(コード, 開始位置, 終了位置) か None。

    「学科 + 番号」が別セルの形と、1 セルに収まった形の両方に対応する。

    **左から探す**のが要点。履修予定表には科目コードのあとに種別と節
    ("Lec"/"A"、"Sem"/"1")が並ぶ列があり、右から探すと "SEM 1" を科目
    コードと取り違える。科目コードの列は必ずそれらより左にある。
    """
    for i in range(0, before - 1):
        dept, num = cells[i].strip(), cells[i + 1].strip()
        if DEPT_RE.match(dept.upper()) and COURSE_NUM_RE.match(num.upper()):
            return f"{dept.upper()} {num.upper()}", i, i + 1
    for i in range(0, before):
        if COURSE_CODE_RE.match(cells[i].strip().upper()):
            return _clean(cells[i].upper()), i, i
    return None


def _parse_schedule_row(cells: list[str]) -> dict | None:
    """履修予定表(Study List)の 1 行 → 履修 1 件。違えば None。

    成績欄がまだ無いので、単位数を手がかりにする。セクション番号(5 桁)や
    時限は単位数として拾わないよう、小数つきの値を優先する。
    """
    if len(cells) < 2 or TOTALS_RE.match(cells[0].strip()):
        return None

    # まず「小数つきの単位数」を探し、無ければ 1〜2 桁の整数を単位数とみなす
    units = units_idx = None
    for pattern in (DECIMAL_UNITS_RE, UNITS_RE):
        for i, cell in enumerate(cells):
            if not pattern.match(cell.strip()):
                continue
            value = float(cell)
            if 0 < value <= 25 and _find_course_code(cells, i):
                units, units_idx = value, i
                break
        if units is not None:
            break
    if units is None:
        return None

    found = _find_course_code(cells, units_idx)
    if not found:
        return None
    code, code_start, code_end = found

    # 単位数の直後に成績があるなら使う(成績表を読ませたときのため)
    grade = IN_PROGRESS
    if units_idx + 1 < len(cells) and GRADE_RE.match(cells[units_idx + 1].strip().upper()):
        grade = cells[units_idx + 1].strip().upper()

    candidates = [cells[i] for i in range(0, code_start)] + \
                 [cells[i] for i in range(code_end + 1, units_idx)]
    candidates = [c.strip() for c in candidates
                  if c.strip() and not UNITS_RE.match(c.strip())
                  and not DECIMAL_UNITS_RE.match(c.strip())]
    title = max(candidates, key=len) if candidates else ""

    return {"code": code, "title": title, "units": units, "grade": grade}


def parse_schedule_html(html: str, school_name: str | None = None) -> dict:
    """履修予定表(Study List)の HTML → まだ成績が出ていない履修一覧。

    返す形は parse_transcript_html と同じだが、grade は "N/A"(履修中)になる。
    学期が書かれていなければ、今のクォーターのものとして扱う。
    """
    import weeks  # 循環 import を避けるためここで読む

    parser = _Blocks()
    parser.feed(html)
    parser.close()

    courses: list[dict] = []
    seen: set[tuple] = set()
    current_term = None

    for block in parser.blocks:
        if block["type"] == "text":
            term = _normalize_term(block["text"])
            if term:
                current_term = term
            continue

        cells = block["cells"]
        row = _parse_schedule_row(cells)
        if row is None:
            term = _normalize_term(" ".join(c for c in cells if c.strip()))
            if term:
                current_term = term
            continue

        row["term"] = current_term or weeks.term_of()
        key = (row["term"], row["code"])
        if key in seen:          # 同じ科目が複数の表に出ていても 1 件にする
            continue
        seen.add(key)
        courses.append(row)

    if not courses:
        return {"courses": [], "terms": [], "error":
                "この HTML から履修科目を読み取れませんでした。"
                "Student Access の「Study List」ページを保存した .html か"
                "確認してください。下の手入力でも登録できます。"}

    terms = sorted({c["term"] for c in courses}, key=term_sort_key)
    return {"courses": courses, "terms": terms, "error": None}


def compute_gpa(courses: list[dict], school_name: str | None = None,
                official: dict | None = None) -> dict:
    """履修一覧 → GPA と単位数のまとめ。

    official(大学が計算済みの集計値)が渡された場合はそちらを優先する。
    編入単位は個別の履修行として載らないため、自前の合計は公式値より
    少なくなることがあるため。

    返す dict:
        gpa            … 累積 GPA(GPA 対象科目のみ)
        gpa_units      … GPA 計算に入った単位数
        grade_points   … 総グレードポイント(= Σ 単位 × ポイント)
        units_earned   … 取得できた単位数(P/S などの合格も含む)
        units_attempted… 履修した単位数の合計
        remaining      … 卒業まで残り単位数(0 未満にはならない)
        progress_pct   … 卒業単位に対する達成率(0-100)
        is_official    … 大学の公式集計をそのまま出しているか
    """
    school = get_school(school_name)
    goal = school["graduation_units"]

    grade_points = 0.0
    gpa_units = 0.0
    units_earned = 0.0
    units_attempted = 0.0
    units_in_progress = 0.0

    for c in courses:
        units = float(c.get("units") or 0)
        grade = (c.get("grade") or "").strip()

        # 履修中(成績待ち)は GPA にも取得単位にも数えず、別に足す
        if grade.upper() in IN_PROGRESS_GRADES:
            units_in_progress += units
            continue

        units_attempted += units
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

    # 公式の集計があればそれを正とする(編入単位ぶんのずれを避けるため)
    official = official or {}
    is_official = False
    if official.get("gpa"):
        gpa = official["gpa"]
        gpa_units = official.get("gpa_units", gpa_units)
        grade_points = official.get("grade_points", grade_points)
        is_official = True
    if official.get("units_completed"):
        units_earned = official["units_completed"]
        is_official = True

    return {
        "gpa": gpa,
        "gpa_units": round(gpa_units, 2),
        "grade_points": round(grade_points, 3),
        "units_earned": round(units_earned, 2),
        "units_attempted": round(units_attempted, 2),
        # 履修中(成績待ち)の単位。GPA には入らないが、卒業までの見通しには効く
        "units_in_progress": round(units_in_progress, 2),
        "goal_units": goal,
        "remaining": round(max(0.0, goal - units_earned), 2),
        "progress_pct": round(min(100.0, units_earned / goal * 100), 1) if goal else 0.0,
        # 履修中ぶんまで終えたときの進捗(進捗バーの薄い部分)
        "progress_with_ip_pct": round(
            min(100.0, (units_earned + units_in_progress) / goal * 100), 1) if goal else 0.0,
        "is_official": is_official,
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
        in_progress = summary["units_in_progress"]
        out.append({
            "term": term,
            "courses": sorted(rows, key=lambda r: r["code"]),
            "gpa": summary["gpa"],
            # 見出しに出す単位数は履修中ぶんも含めた合計
            "units": round(summary["units_attempted"] + in_progress, 2),
            "units_in_progress": in_progress,
            # 成績待ちが 1 件でもあれば、その学期は「履修中」として扱う
            "is_in_progress": in_progress > 0,
        })
    out.sort(key=lambda t: term_sort_key(t["term"]), reverse=True)
    return out
