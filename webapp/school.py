"""学校ごとの学事情報(今は UCI のみハードコード)。

■ このファイルの役割
「セメスター制かクォーター制か」「卒業に必要な単位数」「履修計画サイトの URL」
「成績の GPA 換算表」は学校ごとに違う定数で、次の場所から参照される:

  - academic.html   … 卒業単位までの進捗バー / AntAlmanac へのボタン
  - transcript.py   … 成績 → GPA ポイントの換算
  - weeks.py (StageB)… クォーター開始日から「第 N 週」を出すときの前提

学校を増やすときは SCHOOLS に 1 エントリ足すだけで済むよう、1 ファイルに
まとめてある。ロジックは置かない(定数と、それを引く小さな関数だけ)。

■ 現状の割り切り
DEFAULT_SCHOOL = "UCI" 固定。profiles.school に何が入っていても get_school()
は UCI を返す。複数校対応するときは get_school() の中で profiles.school を
見て切り替える。
"""

# 成績記号 → GPA ポイント。ここに無い記号は「GPA 計算に入れない」扱い。
# (P/NP, S/U, I, IP, W, NR などは単位には数えても GPA には影響しない)
UCI_GRADE_POINTS = {
    "A+": 4.0, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0,
}

# GPA には入らないが「単位は取得できた」と数える記号
UCI_PASSING_NON_GPA = {"P", "S", "CR"}

SCHOOLS = {
    "UCI": {
        "name": "University of California, Irvine",
        "short": "UCI",
        # "quarter" か "semester"。週番号の計算やタブの見出しで使う。
        "term_system": "quarter",
        # 1 学年に並ぶ学期名(Summer は任意履修なので最後)
        "terms": ["Fall", "Winter", "Spring", "Summer"],
        # 学士号の卒業に必要な最低単位数(UCI はクォーター単位で 180)
        "graduation_units": 180,
        # 履修計画サイト。ダッシュボード / GPA ページのボタンから開く。
        "planner_name": "AntAlmanac",
        "planner_url": "https://antalmanac.com",
        # 成績表(Unofficial Transcript)を HTML で保存する手順の案内先
        "transcript_source": "UCI Student Access → Unofficial Transcript",
        "transcript_help": [
            "Student Access にログインする",
            "「Unofficial Transcript」を開く",
            "ページを保存する(⌘S / Ctrl+S)→ .html ファイルができる",
            "その .html をここにアップロードする",
        ],
        "grade_points": UCI_GRADE_POINTS,
        "passing_non_gpa": UCI_PASSING_NON_GPA,
    },
}

DEFAULT_SCHOOL = "UCI"


def get_school(name: str | None = None) -> dict:
    """学校情報を引く。未知の名前・None のときは既定(UCI)を返す。

    今は複数校に対応していないため、事実上つねに UCI が返る。
    """
    if name:
        key = name.strip().upper()
        if key in SCHOOLS:
            return SCHOOLS[key]
    return SCHOOLS[DEFAULT_SCHOOL]


def grade_to_points(grade: str, school: dict | None = None) -> float | None:
    """成績記号 → GPA ポイント。GPA 計算に入れない記号なら None。"""
    school = school or SCHOOLS[DEFAULT_SCHOOL]
    return school["grade_points"].get(grade.strip().upper())


def counts_as_earned(grade: str, school: dict | None = None) -> bool:
    """その成績で単位を取得できたか(卒業単位の進捗に数えてよいか)。"""
    school = school or SCHOOLS[DEFAULT_SCHOOL]
    g = grade.strip().upper()
    if g in school["passing_non_gpa"]:
        return True
    pts = school["grade_points"].get(g)
    # D- (0.7) 以上で単位取得。F は不可。
    return pts is not None and pts > 0
