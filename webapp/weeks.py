"""「第 N 週」と「どのクォーターか」を決める。

■ このファイルの役割
ノートの名前を `Week {N}: {課題名}` の形にするために、次の 2 つを出す:

  week_of(name, date)  … その課題が第何週のものか
  term_of(date)        … その日がどのクォーターに属するか("Fall 2026" など)

ノート一覧のクォーター別タブと、「今週の課題」の絞り込みに使う。

■ 週番号の決め方(この順で、先に決まったものを使う)
  1. **Canvas の書き方をそのまま使う** — 課題名やモジュール名に "Week 3" や
     "Wk3" と書いてあれば、それが正。学生が「第3週」と言うときの基準は
     Canvas の表記なので、こちらを最優先にする。
  2. 書いていなければ、クォーター開始日からの経過日数で計算する。

■ クォーターの区切り
UCI の学事暦は年ごとに数日ずれるが、週番号の用途(課題をまとめる見出し)には
その精度で十分なので、代表的な開始日を定数で持つ。正確な日付が要るように
なったら QUARTER_STARTS を年ごとの表に差し替える。
"""
import re
from datetime import date, datetime, timedelta

# 課題名・モジュール名に埋め込まれた週番号。
# "Week 3" "Week3" "Wk 3" "WEEK 03" "第3週" のいずれにも当たる。
# 語尾に \b を置かないのは "Week3" のように数字が直に続く形を拾うため
# (k と 3 の間には単語境界が無い)。
WEEK_IN_NAME_RE = re.compile(
    r"(?:\bweeks?|\bwk|第)\s*[.:\-]?\s*(\d{1,2})\s*週?", re.IGNORECASE)

# クォーターの開始 (月, 日)。UCI の授業開始日のおおよその位置。
# 「この日以降・次のクォーターの開始日より前」がそのクォーター。
QUARTER_STARTS = [
    ("Winter", (1, 6)),
    ("Spring", (3, 28)),
    ("Summer", (6, 22)),
    ("Fall", (9, 22)),
]

# 1 クォーターの最大週数(10 週 + 期末週)。これを超えたら丸める。
MAX_WEEKS = 11


def _as_date(value) -> date:
    """date / datetime / ISO 文字列 のどれでも date にそろえる。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        # "2026-07-31T12:34:56" でも "2026-07-31" でも先頭 10 文字で足りる
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return date.today()


def week_from_name(name: str | None) -> int | None:
    """課題名やモジュール名に書かれた週番号。無ければ None。

    Canvas に "Week 3 — Reading" と書いてあれば 3 を返す。これが最優先。
    """
    if not name:
        return None
    m = WEEK_IN_NAME_RE.search(name)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= MAX_WEEKS else None


def term_of(when=None) -> str:
    """その日が属するクォーター。"Fall 2026" の形で返す。

    年をまたぐ端(1月上旬など)も、開始日の表で自然に決まるようにしてある。
    """
    d = _as_date(when) if when is not None else date.today()

    # その年の開始日のうち、d 以下でいちばん遅いものが属するクォーター
    current = None
    for season, (mo, day) in QUARTER_STARTS:
        if (d.month, d.day) >= (mo, day):
            current = season
    if current is None:
        # 1/1〜1/5 は前年の Fall クォーターの続き(冬休み明け前)
        return f"Fall {d.year - 1}"
    return f"{current} {d.year}"


def term_start(term: str) -> date | None:
    """"Fall 2026" → そのクォーターの開始日。形式が違えば None。"""
    parts = term.split()
    if len(parts) != 2:
        return None
    season, year = parts[0], parts[1]
    for name, (mo, day) in QUARTER_STARTS:
        if name.lower() == season.lower():
            try:
                return date(int(year), mo, day)
            except ValueError:
                return None
    return None


def week_of(name: str | None = None, when=None) -> int:
    """第何週か。名前に書いてあればそれを、無ければ日付から計算する。

    どちらも決まらない場合でも 1 以上 MAX_WEEKS 以下の数を必ず返す
    (ノートの見出しに使うので None を返さない)。
    """
    from_name = week_from_name(name)
    if from_name is not None:
        return from_name

    d = _as_date(when) if when is not None else date.today()
    start = term_start(term_of(d))
    if start is None:
        return 1
    # 週の境目は月曜。クォーターが週の途中(火曜など)に始まっても、その週
    # 全体が第 1 週になるよう、開始日を含む週の月曜から数える。
    start_monday = start - timedelta(days=start.weekday())
    weeks = (d - start_monday).days // 7 + 1
    return max(1, min(MAX_WEEKS, weeks))


def note_title(name: str | None, when=None, week: int | None = None) -> str:
    """ノートの表示名を `Week {N}: {課題名}` の形にそろえる。

    すでにその形になっているものは二重に付けない。課題名が空のときは
    "Week 3" だけを返す。
    """
    n = week if week is not None else week_of(name, when)
    clean = (name or "").strip()

    # 先頭の "Week 3 -" "Week3:" などは重複するので取り除く
    clean = re.sub(r"^\s*(?:weeks?|wk)\s*[.:\-]?\s*\d{1,2}\s*[:\-–—]?\s*", "",
                   clean, flags=re.IGNORECASE).strip()
    # 拡張子は見出しには不要
    clean = re.sub(r"\.(pdf|docx?|pptx?|txt|md|html?)$", "", clean,
                   flags=re.IGNORECASE).strip()

    return f"Week {n}: {clean}" if clean else f"Week {n}"


def is_current_week(name: str | None = None, when=None, today=None) -> bool:
    """その課題が「今週」のものか。今週の課題の絞り込みに使う。"""
    now = _as_date(today) if today is not None else date.today()
    return week_of(name, when) == week_of(None, now) and \
        term_of(when if when is not None else now) == term_of(now)
