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
    throw new Error("Canvasにログインしていないようです。Canvasを開いてログインしてください。");
  }
  if (!resp.ok) throw new Error(`Canvas API ${resp.status}`);
  return resp.json();
}

async function fetchActiveCourses() {
  const data = await canvasGet("/api/v1/courses?enrollment_state=active&per_page=100");
  if (!Array.isArray(data)) return [];
  return data
    .filter((c) => c && c.id)
    .map((c) => ({ id: c.id, name: c.name || c.course_code || `Course ${c.id}` }));
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
  if (!state.token) return { ok: false, error: "トークン未設定" };
  if (!state.syncSyllabus) return { ok: false, error: "自動同期がオフです" };

  await setStatus("同期中...");
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

    const msg = `✅ ${result.courses}科目を同期しました (${reason}・${result.mode})`;
    await setStatus(msg);
    await chrome.storage.local.set({ lastSync: new Date().toISOString() });
    return { ok: true, courses: result.courses, mode: result.mode };
  } catch (e) {
    const msg = "同期に失敗: " + String(e.message || e);
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

  const courses = await fetchActiveCourses();
  const payload = { courses: [] };
  const courseMap = {};

  for (const course of courses) {
    courseMap[course.name] = course.id;
    const entry = { name: course.name, canvas_id: course.id };
    if (!alreadySynced.has(course.name)) {
      try {
        const syl = await fetchSyllabus(course.id);
        entry.syllabus = syl.text;
        entry.syllabus_links = syl.links;
        entry.syllabus_is_thin = syl.isThin;
      } catch (e) {}
    }
    try { entry.assignments = await fetchAssignments(course.id); } catch (e) {}
    payload.courses.push(entry);
  }

  await apiPost("/api/extension/sync", token, payload);
  await chrome.storage.local.set({ courseMap, courseMapSavedAt: Date.now() });
  return { courses: courses.length, mode: "初回フルクロール" };
}

// 2回目以降: 記憶済みの科目IDを使い、課題の更新だけを直接確認する。
// シラバスは初回で取得済みなので触らない(=そのページには二度と行かない)。
async function incrementalSync(token, courseMap) {
  const payload = { courses: [] };
  for (const [name, canvasId] of Object.entries(courseMap)) {
    const entry = { name, canvas_id: canvasId };
    try { entry.assignments = await fetchAssignments(canvasId); } catch (e) { continue; }
    payload.courses.push(entry);
  }
  await apiPost("/api/extension/sync", token, payload);
  return { courses: payload.courses.length, mode: "差分更新(記憶した場所へ直接)" };
}

// 記憶した科目一覧を強制的に忘れて、次回フルクロールし直す(手動リセット用)
async function forgetCourseMap() {
  await chrome.storage.local.remove(["courseMap", "courseMapSavedAt"]);
}

async function setStatus(text) {
  await chrome.storage.local.set({ lastStatus: text, lastStatusAt: Date.now() });
}

// ---------------------------------------------------------- 起動・定期実行
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(SYNC_ALARM, { periodInMinutes: SYNC_PERIOD_MIN });
});
chrome.runtime.onStartup.addListener(() => { runSync("起動時"); });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === SYNC_ALARM) runSync("定期実行");
});

// トグルがオンに切り替わった瞬間にも一度走らせる
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.syncSyllabus &&
      changes.syncSyllabus.newValue === true) {
    runSync("オンにした直後");
  }
});

// ------------------------------------------------------ メッセージ受信
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.action === "syncNow") {
        sendResponse(await runSync("手動実行"));
        return;
      }
      if (msg.action === "forceRecrawl") {
        await forgetCourseMap();
        sendResponse(await runSync("再取得"));
        return;
      }

      const { token } = await chrome.storage.sync.get({ token: "" });
      if (!token) throw new Error("トークンが未設定です。");

      if (msg.action === "getAnswer") {
        const data = await apiPost("/api/extension/answer", token, {
          course_name: msg.courseName,
          question_text: msg.questionText,
        });
        sendResponse({ ok: true, data });
      } else if (msg.action === "capturePage") {
        const data = await apiPost("/api/extension/capture", token, {
          url: msg.url, title: msg.title, text: msg.text,
          course_name: msg.courseName || "", kind: msg.kind || "resource",
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
      runSync("接続直後");
    });
    return true;
  }
  sendResponse({ ok: false, error: "unknown action" });
});