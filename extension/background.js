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

// 学期の一覧を返す。今日が期間内の学期には isCurrent を立てる。
async function fetchTerms() {
  const courses = await fetchActiveCourses();
  const byName = new Map();
  const now = Date.now();
  for (const c of courses) {
    if (!byName.has(c.term)) {
      let isCurrent = false;
      if (c.termStart && c.termEnd) {
        const s = Date.parse(c.termStart), e = Date.parse(c.termEnd);
        isCurrent = !isNaN(s) && !isNaN(e) && now >= s && now <= e;
      }
      byName.set(c.term, { name: c.term, count: 0, isCurrent });
    }
    byName.get(c.term).count += 1;
  }
  return Array.from(byName.values());
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
    description: htmlToText(a.description || "").slice(0, 2000),
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
    const { courseMap } = await chrome.storage.local.get({ courseMap: null });

    let result;
    if (!courseMap) {
      // 初回のみ: 科目一覧・シラバス・課題を全部巡回して構造を記憶する
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

// 初回フルクロール: 科目一覧を取得し、各科目のシラバス・課題を巡回する。
// 終わったら「科目名 -> Canvas科目ID」の対応表を記憶し、次回以降は
// この対応表を使って直接アクセスする(科目一覧の再取得すら省く)。
async function fullCrawl(token) {
  let known = { synced_syllabi: [] };
  try { known = await apiGet("/api/extension/state", token); } catch (e) {}
  const alreadySynced = new Set(known.synced_syllabi || []);

  const { selectedTerm } = await chrome.storage.sync.get({ selectedTerm: "" });
  let courses = await fetchActiveCourses();
  // 学期が指定されていればその学期だけに絞る(空文字なら全学期)
  if (selectedTerm) {
    courses = courses.filter((c) => c.term === selectedTerm);
  }
  const payload = { courses: [] };
  const courseMap = {};
  await progressInit("Full crawl", courses.map((c) => c.name));

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
    } catch (e) { failed = true; }

    payload.courses.push(entry);
    await progressUpdate(i, failed ? "error" : "done",
      failed ? "could not read some data" : parts.join(" · "));
  }

  await apiPost("/api/extension/sync", token, payload);
  await chrome.storage.local.set({ courseMap, courseMapSavedAt: Date.now() });
  await progressFinish();
  return { courses: courses.length, mode: "Full crawl" };
}

// 2回目以降: 記憶済みの科目IDを使い、課題の更新だけを直接確認する。
// シラバスは初回で取得済みなので触らない(=そのページには二度と行かない)。
async function incrementalSync(token, courseMap) {
  const payload = { courses: [] };
  const names = Object.keys(courseMap);
  await progressInit("Incremental (using saved course list)", names);

  for (let i = 0; i < names.length; i++) {
    const name = names[i];
    await progressUpdate(i, "running", "checking assignments...");
    const entry = { name, canvas_id: courseMap[name] };
    try {
      entry.assignments = await fetchAssignments(courseMap[name]);
      payload.courses.push(entry);
      await progressUpdate(i, "done", `${entry.assignments.length} assignment(s)`);
    } catch (e) {
      await progressUpdate(i, "error", "could not read assignments");
    }
  }

  await apiPost("/api/extension/sync", token, payload);
  await progressFinish();
  return { courses: payload.courses.length,
           mode: "Incremental (using saved course list)" };
}

// 記憶した科目一覧を強制的に忘れて、次回フルクロールし直す(手動リセット用)
async function forgetCourseMap() {
  await chrome.storage.local.remove(["courseMap", "courseMapSavedAt"]);
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
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(SYNC_ALARM, { periodInMinutes: SYNC_PERIOD_MIN });
  chrome.alarms.create(NOTIFY_ALARM, { periodInMinutes: NOTIFY_PERIOD_MIN });
});
chrome.runtime.onStartup.addListener(() => { runSync("on startup"); });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === SYNC_ALARM) runSync("scheduled");
  if (alarm.name === NOTIFY_ALARM) pollNotifications();
});

// トグルがオンに切り替わった瞬間にも一度走らせる
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync") return;
  // 学期を変えたら、記憶していた科目一覧は無効なので作り直す
  if (changes.selectedTerm) {
    forgetCourseMap().then(() => runSync("term changed"));
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
      if (msg.action === "getTerms") {
        try {
          sendResponse({ ok: true, terms: await fetchTerms() });
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