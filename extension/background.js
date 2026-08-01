// バックグラウンドの service worker。ここが「自動モード」の本体。
//
// 仕組み:
//   学生はすでにブラウザでCanvasにログインしている。host_permissions に
//   canvas.eee.uci.edu を入れてあるので、ここから fetch すると
//   そのログインCookieが自動で付く。つまりトークンも2FAも不要で、
//   Canvasの公式APIから構造化データ(JSON)を直接取得できる。
//
// 自動モードがオンの間:
//   - 起動時 / 定期的(6時間ごと)/ 手動実行 で同期する
//   - サーバーに「すでに持っている情報」を問い合わせ、無駄な取得を避ける
//   - 取得したシラバス・課題をサーバーへ送る

const API_BASE = "https://study-agent-500j.onrender.com";
const CANVAS_BASE = "https://canvas.eee.uci.edu";
const SYNC_ALARM = "studyAgentSync";
const SYNC_PERIOD_MIN = 360; // 6時間
const NOTIFY_ALARM = "studyAgentNotify";
const NOTIFY_PERIOD_MIN = 30;   // 下書きができたかを見に行く間隔
const WEEK_ALARM = "studyAgentWeek";  // 毎週月曜、新しい週の始まり

// スクリーンショットを撮らないサイト。
// Canvas は「学生自身が操作する」と決めてあるので、こちらからは触らない。
const SCREENSHOT_BLOCKED = ["canvas.eee.uci.edu", "instructure.com"];

function screenshotBlocked(url) {
  try {
    const host = new URL(url).hostname;
    return SCREENSHOT_BLOCKED.some((h) => host.includes(h));
  } catch (e) {
    return true;   // URL が読めないページ(chrome:// など)では撮らない
  }
}

// ---------------------------------------------------------- サーバー通信
async function apiPost(path, token, body) {
  const resp = await fetch(API_BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json",
               "Authorization": "Bearer " + token },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`API error ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp.json();
}

async function apiGet(path, token) {
  const resp = await fetch(API_BASE + path, {
    headers: { "Authorization": "Bearer " + token },
  });
  if (!resp.ok) throw new Error(`API error ${resp.status}`);
  return resp.json();
}

// ------------------------------------------------------- Canvas API取得
// credentials: "include" で学生の既存ログインCookieを使う(トークン不要)
async function canvasGet(path) {
  const resp = await fetch(CANVAS_BASE + path, {
    credentials: "include",
    headers: { "Accept": "application/json" },
  });
  if (resp.status === 401 || resp.status === 403) {
    throw new Error("You do not appear to be logged into Canvas. Open Canvas and sign in.");
  }
  if (!resp.ok) throw new Error(`Canvas API ${resp.status}`);
  return resp.json();
}

async function fetchActiveCourses() {
  const data = await canvasGet(
    "/api/v1/courses?enrollment_state=active&per_page=100&include[]=term");
  if (!Array.isArray(data)) return [];
  return data
    .filter((c) => c && c.id)
    .map((c) => ({
      id: c.id,
      name: c.name || c.course_code || `Course ${c.id}`,
      term: (c.term && c.term.name) || "(no term)",
      termStart: (c.term && c.term.start_at) || null,
      termEnd: (c.term && c.term.end_at) || null,
    }));
}

async function fetchSyllabus(courseId) {
  const data = await canvasGet(`/api/v1/courses/${courseId}?include[]=syllabus_body`);
  const html = data.syllabus_body || "";
  const text = htmlToText(html);
  const links = extractOutboundLinks(html);
  // シラバス本文が極端に短くリンクだけの場合、本体は外部サイトにあるとみなす
  const isThin = text.length < 300 && links.length > 0;
  return { text, links, isThin };
}

// シラバス本文からリンクを抜き出す。相対URLは絶対URLに直す。
// Canvasの課題/クイズ等、既にAPIで取得済みのものへのリンクは除外する。
function extractOutboundLinks(html) {
  if (!html) return [];
  const out = [];
  const seen = new Set();
  const re = /<a\s+[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    let href = (m[1] || "").trim();
    if (!href || href.startsWith("#") || /^(mailto|javascript|tel):/i.test(href)) continue;
    let abs;
    try { abs = new URL(href, CANVAS_BASE); } catch (e) { continue; }
    if (!/^https?:$/.test(abs.protocol)) continue;
    if (isAlreadySyncedCanvasPath(abs)) continue;
    if (seen.has(abs.href)) continue;
    seen.add(abs.href);
    const label = htmlToText(m[2] || "").trim();
    out.push({ url: abs.href, label: label || abs.hostname });
    if (out.length >= 20) break;
  }
  return out;
}

function isAlreadySyncedCanvasPath(u) {
  let canvasOrigin;
  try { canvasOrigin = new URL(CANVAS_BASE).origin; } catch (e) { return false; }
  if (u.origin !== canvasOrigin) return false; // 外部サイトは常に対象
  // 課題・クイズ・掲示板・モジュールは別途APIで取得済みなので除外
  return /\/(assignments|quizzes|discussion_topics|modules|grades)\b/.test(u.pathname);
}

async function fetchAssignments(courseId) {
  const data = await canvasGet(
    `/api/v1/courses/${courseId}/assignments?per_page=100&order_by=due_at`);
  if (!Array.isArray(data)) return [];
  return data.filter((a) => a && a.id).map((a) => ({
    id: a.id,
    name: a.name || "",
    due_at: a.due_at || null,
    points: a.points_possible ?? null,
    url: a.html_url || null,
    // Canvas 側の最終更新時刻。次回の同期でこれを比べて、変わっていない
    // 課題は送らない(= サーバーも書かない)。差分同期の要。
    updated_at: a.updated_at || null,
    description: htmlToText(a.description || "").slice(0, 2000),
    rubric: rubricToText(a.rubric),
  }));
}

// ルーブリック(採点基準)を読める文にする。
// Canvas は課題にルーブリックが付いているときだけ rubric 配列を返すので、
// 無いことのほうが多い。無ければ空文字。
function rubricToText(rubric) {
  if (!Array.isArray(rubric) || rubric.length === 0) return "";
  const lines = [];
  for (const r of rubric.slice(0, 30)) {
    if (!r) continue;
    const pts = r.points != null ? ` (${r.points} pts)` : "";
    lines.push(`- ${htmlToText(r.description || "(no name)")}${pts}`);
    const long = htmlToText(r.long_description || "").trim();
    if (long) lines.push(`    ${long.replace(/\n+/g, " ").slice(0, 300)}`);
  }
  return lines.join("\n").slice(0, 4000);
}

// 週ごとのモジュール構成。「Week 3 — Sorting」のような並びが取れるので、
// 課題名に週が書かれていない科目でも週の見当がつく。
async function fetchModules(courseId) {
  const data = await canvasGet(
    `/api/v1/courses/${courseId}/modules?include[]=items&per_page=50`);
  if (!Array.isArray(data)) return [];
  return data.filter((m) => m && m.id).map((m) => ({
    id: m.id,
    name: m.name || "",
    position: m.position ?? null,
    items: (m.items || []).slice(0, 40).map((it) => ({
      title: it.title || "", type: it.type || "", url: it.html_url || null,
    })),
  }));
}

const NAMED_ENTITIES = {
  nbsp: " ", amp: "&", lt: "<", gt: ">", quot: '"', apos: "'",
  ndash: "\u2013", mdash: "\u2014", hellip: "\u2026", rsquo: "\u2019",
  lsquo: "\u2018", ldquo: "\u201C", rdquo: "\u201D", bull: "\u2022",
  middot: "\u00B7", times: "\u00D7", deg: "\u00B0", trade: "\u2122",
  copy: "\u00A9", reg: "\u00AE",
};

function decodeEntities(s) {
  // 数値参照 (&#37; / &#x2013;)
  s = s.replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)));
  s = s.replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)));
  // 名前付き参照
  return s.replace(/&([a-z]+);/gi, (m, name) => {
    const v = NAMED_ENTITIES[name.toLowerCase()];
    return v !== undefined ? v : m;
  });
}

function htmlToText(html) {
  if (!html) return "";
  let t = html.replace(/<(br|\/p|\/li|\/div|\/h[1-6])\s*\/?>/gi, "\n");
  t = t.replace(/<li[^>]*>/gi, "- ");
  t = t.replace(/<[^>]+>/g, "");
  t = decodeEntities(t);
  return t.replace(/\n{3,}/g, "\n\n").trim();
}

// ---------------------------------------------------------- 同期本体
async function runSync(reason = "manual") {
  const state = await chrome.storage.sync.get(
    { syncSyllabus: false, token: "" });
  if (!state.token) return { ok: false, error: "No token set" };
  if (!state.syncSyllabus) return { ok: false, error: "Auto sync is off" };

  await setStatus("Syncing...");
  try {
    const { courseMap, courseMapKey } = await chrome.storage.local.get(
      { courseMap: null, courseMapKey: null });
    const { selectedCourseIds } = await chrome.storage.sync.get(
      { selectedCourseIds: [] });

    // 1 つも選ばれていなければ何もしない。「選んでいない = 全部」にすると、
    // オリエンテーションや去年の授業まで巻き込んでしまう。
    if (!selectedCourseIds || selectedCourseIds.length === 0) {
      const msg = "Pick which courses to sync in the popup first.";
      await setStatus(msg);
      return { ok: false, error: msg };
    }

    let result;
    if (needsFullCrawl(courseMap, courseMapKey, selectedCourseIds)) {
      // 科目一覧・シラバス・課題を全部巡回して構造を記憶する
      result = await fullCrawl(state.token);
    } else {
      // 2回目以降: 記憶した科目IDに直接アクセスするだけ。
      // 科目一覧の再取得もシラバスページの再訪問もしない。
      result = await incrementalSync(state.token, courseMap);
    }

    const msg = `✓ Synced ${result.courses} course(s) — ${result.mode} (${reason})`;
    await setStatus(msg);
    await chrome.storage.local.set({ lastSync: new Date().toISOString() });
    return { ok: true, courses: result.courses, mode: result.mode };
  } catch (e) {
    const msg = "Sync failed: " + String(e.message || e);
    await setStatus(msg);
    return { ok: false, error: msg };
  }
}

// 覚えている科目一覧を使ってよいか、作り直すべきかを決める。
//
// **絞り込みが効くのはフルクロールのときだけ。** 差分同期は覚えた対応表を
// そのまま使うので、選ぶ科目を変えても対応表が古いままだと、外した科目が
// 同期され続ける。そこで対応表には「どの選択で作ったか」を添えてあり、
// 選択が変わっていれば作り直す。
//
// courseMapKey が無い = 選択を記録する前に作られた古い対応表。中身が
// 信用できないので作り直す。
function selectionKey(ids) {
  // n > 0 まで見るのは、Number(null) が 0、Number("") も 0 になるため。
  // NaN だけ落とすと null が「id 0」として残り、選択が変わっていないのに
  // 別の鍵になってしまう。Canvas の科目 ID は必ず正の整数。
  return (ids || []).map(Number)
    .filter((n) => Number.isFinite(n) && n > 0)
    .sort((a, b) => a - b).join(",");
}

function needsFullCrawl(courseMap, courseMapKey, selectedCourseIds) {
  if (!courseMap) return true;
  if (courseMapKey === null || courseMapKey === undefined) return true;
  return courseMapKey !== selectionKey(selectedCourseIds);
}

// 初回フルクロール: 科目一覧を取得し、各科目のシラバス・課題を巡回する。
// 終わったら「科目名 -> Canvas科目ID」の対応表を記憶し、次回以降は
// この対応表を使って直接アクセスする(科目一覧の再取得すら省く)。
async function fullCrawl(token) {
  let known = { synced_syllabi: [] };
  try { known = await apiGet("/api/extension/state", token); } catch (e) {}
  const alreadySynced = new Set(known.synced_syllabi || []);

  const { selectedCourseIds } = await chrome.storage.sync.get(
    { selectedCourseIds: [] });
  const wanted = new Set((selectedCourseIds || []).map(Number));
  const everything = await fetchActiveCourses();
  // **選んだ科目だけ。** 学期で自動的に絞るのはやめた — Canvas は
  // オリエンテーション用のスペースも、年をまたぐ研修コースも、普通の授業と
  // まったく同じ形で返してくるうえ、学期名が付いていないものも多いので、
  // 機械的に「今年のぶん」を見分けられなかった。
  const courses = everything.filter((c) => wanted.has(Number(c.id)));

  if (courses.length === 0) {
    await progressInit("Nothing selected", []);
    await progressFinish();
    throw new Error(
      "None of the courses you picked are in Canvas any more. Open the popup and pick again.");
  }

  // full: true = 「同期する科目はこれで全部」。サーバーはこれを見て、
  // ここに出てこない科目(外した科目・前の学期のもの)を片付ける。
  const payload = { courses: [], full: true };
  const courseMap = {};
  const skipped = everything.length - courses.length;
  await progressInit(
    `Full crawl — ${courses.length} picked${skipped ? ` (${skipped} not picked)` : ""}`,
    courses.map((c) => c.name));

  for (let i = 0; i < courses.length; i++) {
    const course = courses[i];
    courseMap[course.name] = course.id;
    const entry = { name: course.name, canvas_id: course.id };
    const parts = [];
    let failed = false;

    await progressUpdate(i, "running", "reading syllabus...");
    if (!alreadySynced.has(course.name)) {
      try {
        const syl = await fetchSyllabus(course.id);
        entry.syllabus = syl.text;
        entry.syllabus_links = syl.links;
        entry.syllabus_is_thin = syl.isThin;
        parts.push(syl.isThin ? "syllabus is off-site" : "syllabus");
        if (syl.links.length) parts.push(`${syl.links.length} link(s)`);
      } catch (e) { failed = true; }
    } else {
      parts.push("syllabus already saved");
    }

    await progressUpdate(i, "running", "reading assignments...");
    try {
      entry.assignments = await fetchAssignments(course.id);
      parts.push(`${entry.assignments.length} assignment(s)`);
      const withRubric = entry.assignments.filter((a) => a.rubric).length;
      if (withRubric) parts.push(`${withRubric} rubric(s)`);
    } catch (e) { failed = true; }

    // 週ごとの構成。取れない科目(モジュールを使っていない)もあるので、
    // 失敗しても failed 扱いにはしない。
    await progressUpdate(i, "running", "reading weekly modules...");
    try {
      entry.modules = await fetchModules(course.id);
      if (entry.modules.length) parts.push(`${entry.modules.length} module(s)`);
    } catch (e) { entry.modules = []; }

    payload.courses.push(entry);
    await progressUpdate(i, failed ? "error" : "done",
      failed ? "could not read some data" : parts.join(" · "));
  }

  const res = await apiPost("/api/extension/sync", token, payload);
  // どの選択で作った一覧かも一緒に覚える。次の同期でこれを見て、選択が
  // 変わっていたら作り直す(差分同期が外した科目を引きずらないように)。
  await chrome.storage.local.set({ courseMap,
                                   courseMapKey: selectionKey(selectedCourseIds),
                                   courseMapSavedAt: Date.now() });
  await progressFinish();

  // 前の学期の科目が片付いたら、そのことも出す(黙って消さない)
  const dropped = (res && res.pruned) || [];
  let mode = `Full crawl — ${courses.length} picked course(s)`;
  if (dropped.length) mode += ` · removed ${dropped.length} you no longer sync`;
  return { courses: courses.length, mode };
}

// 2回目以降: 記憶済みの科目IDを使い、課題の更新だけを直接確認する。
// シラバスは初回で取得済みなので触らない(=そのページには二度と行かない)。
//
// さらに、サーバーが持っている課題の updated_at を先に聞いてから比べ、
// **変わった課題だけ** を送る。1件も変わっていない科目は POST 自体を省く。
// Canvas への問い合わせは科目ごとに1回必要(何が変わったかは聞かないと
// 分からないため)だが、そこから先の送信・DB書き込み・下書き生成は
// 変更があったぶんだけになる。
async function incrementalSync(token, courseMap) {
  let state = {};
  try { state = await apiGet("/api/extension/state", token); } catch (e) {}
  const known = state.assignment_state || {};

  const payload = { courses: [] };
  const names = Object.keys(courseMap);
  await progressInit("Incremental (changed assignments only)", names);

  let sent = 0, skipped = 0;
  for (let i = 0; i < names.length; i++) {
    const name = names[i];
    await progressUpdate(i, "running", "checking assignments...");
    const seen = known[name] || {};
    try {
      const all = await fetchAssignments(courseMap[name]);
      // updated_at が同じものは「前に取ったまま」なので送らない。
      // updated_at が無い課題は判断できないので、安全側に倒して送る。
      const changed = all.filter((a) => {
        if (!a.updated_at) return true;
        return seen[String(a.id)] !== a.updated_at;
      });
      skipped += all.length - changed.length;

      if (changed.length === 0) {
        await progressUpdate(i, "skipped",
          `no change · ${all.length} assignment(s) already current`);
        continue;
      }
      sent += changed.length;
      payload.courses.push({ name, canvas_id: courseMap[name],
                             assignments: changed, partial: true });
      await progressUpdate(i, "done",
        `${changed.length} new/updated · ${all.length - changed.length} unchanged`);
    } catch (e) {
      await progressUpdate(i, "error", "could not read assignments");
    }
  }

  if (payload.courses.length === 0) {
    await progressFinish();
    return { courses: 0,
             mode: `Incremental — nothing changed (${skipped} already current)` };
  }

  await apiPost("/api/extension/sync", token, payload);
  await progressFinish();
  return { courses: payload.courses.length,
           mode: `Incremental — ${sent} changed, ${skipped} skipped` };
}

// 記憶した科目一覧を強制的に忘れて、次回フルクロールし直す(手動リセット用)
async function forgetCourseMap() {
  await chrome.storage.local.remove(
    ["courseMap", "courseMapKey", "courseMapSavedAt"]);
}

async function setStatus(text) {
  await chrome.storage.local.set({ lastStatus: text, lastStatusAt: Date.now() });
}

// ---------------------------------------------------------------- progress
// The popup mirrors this object live, so the user can watch each course
// being fetched instead of staring at a single "syncing..." line.
let progress = null;

async function progressInit(mode, courseNames) {
  progress = {
    running: true,
    mode,
    total: courseNames.length,
    completed: 0,
    courses: courseNames.map((n) => ({ name: n, status: "pending", detail: "" })),
  };
  await chrome.storage.local.set({ syncProgress: progress });
}

async function progressUpdate(index, status, detail) {
  if (!progress || !progress.courses[index]) return;
  progress.courses[index].status = status;
  if (detail !== undefined) progress.courses[index].detail = detail;
  progress.completed = progress.courses.filter(
    (c) => c.status === "done" || c.status === "error" || c.status === "skipped").length;
  await chrome.storage.local.set({ syncProgress: progress });
}

async function progressFinish() {
  if (!progress) return;
  progress.running = false;
  await chrome.storage.local.set({ syncProgress: progress });
}

// ---------------------------------------------------------- 起動・定期実行
// 次の月曜の 00:05(端末の時計)。週の境目は weeks.py と同じく月曜。
// 00:00 ちょうどを避けて 5 分ずらすのは、日付が変わる瞬間ぴったりだと
// 端末がスリープから起ききっていないことがあるため。
function nextMondayStart() {
  const now = new Date();
  const target = new Date(now);
  target.setHours(0, 5, 0, 0);
  let days = (1 - now.getDay() + 7) % 7;   // 0=日 1=月 … 次の月曜まで何日か
  if (days === 0 && target.getTime() <= now.getTime()) days = 7;
  target.setDate(target.getDate() + days);
  return target.getTime();
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(SYNC_ALARM, { periodInMinutes: SYNC_PERIOD_MIN });
  chrome.alarms.create(NOTIFY_ALARM, { periodInMinutes: NOTIFY_PERIOD_MIN });
  chrome.alarms.create(WEEK_ALARM,
    { when: nextMondayStart(), periodInMinutes: 60 * 24 * 7 });
});
chrome.runtime.onStartup.addListener(() => { runSync("on startup"); });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === SYNC_ALARM) runSync("scheduled");
  if (alarm.name === NOTIFY_ALARM) pollNotifications();
  if (alarm.name === WEEK_ALARM) startOfWeek();
});

// 新しい週の始まり。まず差分同期をして最新の課題を取り込み、その後で
// 「今週の課題」をまとめて1件だけ通知する。**提出は一切しない。**
async function startOfWeek() {
  await runSync("new week");
  const { token } = await chrome.storage.sync.get({ token: "" });
  if (!token) return;

  let data;
  try {
    data = await apiGet("/api/extension/week", token);
  } catch (e) {
    return;   // 通信できなければ黙って次の週を待つ
  }
  const items = data.items || [];
  if (!items.length) return;

  const lines = items.slice(0, 5).map((it) =>
    `• ${it.name}${it.due ? ` — ${it.due}` : ""}`);
  if (items.length > 5) lines.push(`…and ${items.length - 5} more`);

  const id = `sa-week-${data.term || ""}-${data.week_no || 0}`;
  chrome.notifications.create(id, {
    type: "basic",
    iconUrl: "icon128.png",
    title: `Week ${data.week_no} — ${items.length} assignment(s)`,
    message: lines.join("\n"),
    contextMessage: data.term || undefined,
  });
  if (data.url) notificationLinks[id] = data.url;
}

// トグルがオンに切り替わった瞬間にも一度走らせる
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync") return;
  // 選ぶ科目を変えたら、記憶していた対応表は無効なので作り直す
  if (changes.selectedCourseIds) {
    forgetCourseMap().then(() => runSync("course selection changed"));
    return;
  }
  if (changes.syncSyllabus && changes.syncSyllabus.newValue === true) {
    runSync("just enabled");
  }
});

// ------------------------------------------------ スクリーンショット
// アイコンから呼ばれる。activeTab 権限なので、ユーザーが押した瞬間だけ撮れる。
async function captureAndAsk({ mode, prompt, courseName }) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("No active tab.");
  if (screenshotBlocked(tab.url)) {
    // Canvas では撮らない。サーバー側でも同じ判定をしている(二重の歯止め)。
    return { ok: false, blocked: true,
             error: "Screenshots are turned off on Canvas. You drive Canvas yourself." };
  }

  const image = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  const { token } = await chrome.storage.sync.get({ token: "" });
  if (!token) throw new Error("No token set.");

  const data = await apiPost("/api/extension/screenshot", token, {
    image, mode, prompt: prompt || "", url: tab.url || "",
    course_name: courseName || "",
  });
  return { ok: true, data };
}

// ------------------------------------------------ 下書き完了の通知
// **提出はしない。** 「下書きができた」ことだけを知らせる。
// 通知 ID → 開く URL。クリックしたときにその下書きを開くために覚えておく。
const notificationLinks = {};

async function pollNotifications() {
  const { token } = await chrome.storage.sync.get({ token: "" });
  if (!token) return;

  let data;
  try {
    data = await apiGet("/api/extension/notifications", token);
  } catch (e) {
    return;   // 通信できないときは黙って次の周期を待つ
  }

  const pending = data.pending || [];
  if (!pending.length) return;

  for (const item of pending) {
    chrome.notifications.create(`sa-draft-${item.id}`, {
      type: "basic",
      iconUrl: "icon128.png",
      title: "Draft ready",
      message: `${item.name} — review it before you submit anything.`,
      contextMessage: item.course || undefined,
    });
    if (item.url) notificationLinks[`sa-draft-${item.id}`] = item.url;
  }

  // 出し終えたら印を付ける(同じ通知を毎回出さないため)
  await apiPost("/api/extension/notifications/ack", token,
                { ids: pending.map((p) => p.id) });
}

// 通知をクリックしたら、その下書きを開く
chrome.notifications.onClicked.addListener((id) => {
  const url = notificationLinks[id];
  if (url) chrome.tabs.create({ url });
  chrome.notifications.clear(id);
});

// ------------------------------------------------------ メッセージ受信
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.action === "listCanvasCourses") {
        // 小窓の科目チェックリスト用。Canvas から一覧を取るだけで、
        // シラバスも課題もまだ読まない(選ばれてから読む)。
        try {
          sendResponse({ ok: true, courses: await fetchActiveCourses() });
        } catch (e) {
          sendResponse({ ok: false, error: String(e.message || e) });
        }
        return;
      }
      if (msg.action === "screenshot") {
        sendResponse(await captureAndAsk(msg));
        return;
      }
      if (msg.action === "checkNotifications") {
        await pollNotifications();
        sendResponse({ ok: true });
        return;
      }
      if (msg.action === "syncNow") {
        sendResponse(await runSync("manual"));
        return;
      }
      if (msg.action === "forceRecrawl") {
        await forgetCourseMap();
        sendResponse(await runSync("re-fetch"));
        return;
      }

      const { token } = await chrome.storage.sync.get({ token: "" });
      if (!token) throw new Error("No token set.");

      if (msg.action === "getAnswer") {
        const data = await apiPost("/api/extension/answer", token, {
          course_name: msg.courseName,
          question_text: msg.questionText,
        });
        sendResponse({ ok: true, data });
      } else if (msg.action === "reportLinks") {
        // content.js が今開いているページから拾ったリンクを報告するだけ。
        // ここから他のページに追加でアクセスすることはしない。
        const data = await apiPost("/api/extension/links", token, {
          course_name: msg.courseName || "",
          source_url: msg.sourceUrl || "",
          links: msg.links || [],
        });
        sendResponse({ ok: true, data });
      } else if (msg.action === "getCourses") {
        const data = await apiGet("/api/extension/state", token);
        sendResponse({ ok: true, courses: data.courses || [],
                       pendingLinks: data.pending_links || [] });
      } else {
        sendResponse({ ok: false, error: "unknown action" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true;
});

// サイトからのワンクリック接続(manifest の externally_connectable で許可済み)
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (msg.action === "connectToken" && msg.token) {
    chrome.storage.sync.set({ token: msg.token }, () => {
      sendResponse({ ok: true });
      runSync("just connected");
    });
    return true;
  }
  sendResponse({ ok: false, error: "unknown action" });
});